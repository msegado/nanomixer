import numpy as np
from biquads import normalize, filter_types
from dsp_program import (
    HARDWARE_PARAMS,
    mixer,
    constants, meter_filter_param_base, StateVarFilter)
import logging
import time
import json
from datetime import datetime
from spi_channel import SPIChannel
from fpga_data_link import IOThread, SPI_BUF_SIZE_IN_WORDS

logger = logging.getLogger(__name__)

METERING_LPF_PARAMS = dict(
    Fc=7.5,
    Q=np.sqrt(2.)/2.,
    Fs=48000)

WORDS_PER_CORE = 1024 # FIXME !
MIN_FADER = -180.

def pack_biquad_coeffs(b, a):
    return [b[0], b[1], b[2], -a[1], -a[2]]

# Panning
PAN_LAW_DB = 3.
panning_exponent = PAN_LAW_DB / (20*np.log10(2.))


# Just using a class so that state_names.bus works.
class state_names(object):
    bus = 'b{bus}/{param}'
    channel = 'c{channel}/{param}'
    fader = 'b{bus}/c{channel}/{param}'
    channel_filter = 'c{channel}/f{filt}/{param}'
    bus_filter = 'b{bus}/f{filt}/{param}'


logical_bus_to_physical_bus_mapping = [
    [0, 1],
    [2],
    [3],
    [4],
    [5],
    [6],
    [7],
]


def invert_mapping(logical_bus_to_physical_bus_mapping, num_physical_buses):
    logical_bus_for_physical_bus = [None] * num_physical_buses
    for logical_bus, physical_buses in enumerate(logical_bus_to_physical_bus_mapping):
        for physical_bus in physical_buses:
            logical_bus_for_physical_bus[physical_bus] = logical_bus
    return logical_bus_for_physical_bus

num_physical_buses = HARDWARE_PARAMS['num_busses_per_core'] * HARDWARE_PARAMS['num_cores']

logical_bus_for_physical_bus = invert_mapping(logical_bus_to_physical_bus_mapping, num_physical_buses)


metadata = dict(
    num_busses=len(logical_bus_to_physical_bus_mapping),
    num_channels=HARDWARE_PARAMS['num_cores'] * HARDWARE_PARAMS['num_channels_per_core'],
    num_biquads_per_channel=HARDWARE_PARAMS['num_biquads_per_channel'],
    num_biquads_per_bus=HARDWARE_PARAMS['num_biquads_per_bus'])

initial_filter_frequencies = [250, 500, 1000, 6000, 12000]
solo_bus_index = len(logical_bus_to_physical_bus_mapping) - 1


class InvalidSnapshot(Exception):
    pass



def get_initial_state(metadata):
    state = {}

    def set_state_params(name_format, base_kv, **kw):
        for k, v in kw.iteritems():
            name = name_format.format(param=k, **base_kv)
            state[name] = v

    for bus in range(metadata['num_busses']):
        if bus == 0:
            name = "Master"
        elif bus == solo_bus_index:
            name = "Solo"
        else:
            name = "Aux {}".format(bus)
        # Masters
        set_state_params(state_names.bus, dict(bus=bus), name=name, lvl=0., pan=0.)

        # Downmix
        for channel in range(metadata['num_channels']):
            set_state_params(state_names.fader, dict(bus=bus, channel=channel), lvl=MIN_FADER, pan=0.)

        # Bus filters
        for filt, freq in enumerate(initial_filter_frequencies):
            assert metadata['num_biquads_per_bus'] == len(initial_filter_frequencies)
            if filt == 0:
                typ = 'lowshelf'
            elif filt == len(initial_filter_frequencies) - 1:
                typ = 'highshelf'
            else:
                typ = 'peaking'
            set_state_params(state_names.bus_filter, dict(bus=bus, filt=filt),
                type=typ, freq=freq, gain=0., q=np.sqrt(2.)/2)


    for channel in range(metadata['num_channels']):
        assert metadata['num_biquads_per_channel'] == len(initial_filter_frequencies)
        set_state_params(state_names.channel, dict(channel=channel), name="Ch{}".format(channel+1), mute=True, pfl=False)

        for filt, freq in enumerate(initial_filter_frequencies):
            if filt == 0:
                typ = 'lowshelf'
            elif filt == len(initial_filter_frequencies) - 1:
                typ = 'highshelf'
            else:
                typ = 'peaking'
            set_state_params(state_names.channel_filter, dict(channel=channel, filt=filt),
                type=typ, freq=freq, gain=0., q=np.sqrt(2.)/2)

    state['metadata'] = metadata

    return state


import marshal
class OneStepMemoizer(object):
    def __init__(self):
        self.cur = {}
        self.next = {}

    def get(self, func, *a, **kw):
        key = marshal.dumps((a, kw))
        if key in self.cur:
            val = self.cur[key]
        else:
            val = func(*a, **kw)
            self.cur[key] = val
        self.next[key] = val
        return val

    def advance(self):
        self.cur = self.next
        self.next = {}


def compute_biquad_param_mem(typ, **kw):
    b, a = filter_types[typ](**kw)
    b, a = normalize(b, a)
    return pack_biquad_coeffs(b, a)


def logical_to_physical(state, mixer, set_memory, memoizer):
    """
    State comes in as logical, here we figure out how to set_memory in the physical mixer to match.
    """

    def get_state_param(name_format, base_kv, param):
        assert isinstance(param, basestring)
        return state[name_format.format(param=param, **base_kv)]

    def get_state_params(name_format, base_kv, params):
        assert not isinstance(params, basestring)
        return [get_state_param(name_format, base_kv, param) for param in params]

    # TODO: grab this from metadata?
    num_physical_buses = len(mixer.downmixes)

    # Channel filters
    for channel, biquad_chain in enumerate(mixer.channel_biquads):
        for biquad_idx, biquad_params in enumerate(biquad_chain.params):
            typ, freq, gain, q = get_state_params(state_names.channel_filter, dict(channel=channel, filt=biquad_idx), ['type', 'freq', 'gain', 'q'])

            set_memory(
                core=0,  # hardcoded, until we can test multi-core and get the right abstraction.
                addr=biquad_params[0].addr,
                data=memoizer.get(compute_biquad_param_mem, typ=typ, f0=freq, dBgain=gain, q=q))

    # Bus filters
    for physical_bus, bus_strip in enumerate(mixer.bus_strips):
        for biquad_idx, biquad_params in enumerate(bus_strip.biquad_chain.params):
            logical_bus = logical_bus_for_physical_bus[physical_bus]
            typ, freq, gain, q = get_state_params(state_names.bus_filter, dict(bus=logical_bus, filt=biquad_idx), ['type', 'freq', 'gain', 'q'])

            set_memory(
                core=0,  # hardcoded, until we can test multi-core and get the right abstraction.
                addr=biquad_params[0].addr,
                data=memoizer.get(compute_biquad_param_mem, typ=typ, f0=freq, dBgain=gain, q=q))

    # Downmix buses
    num_downmix_channels = len(mixer.downmixes[0].gain[0]) # FIXME: hardcoded core 0.
    gain_for_physical_bus = np.zeros((num_physical_buses, num_downmix_channels))

    for logical_bus, physical_buses in enumerate(logical_bus_to_physical_bus_mapping):
        bus_output_level = get_state_param(state_names.bus, dict(bus=logical_bus), 'lvl')
        absBusFaderLevel = 10. ** (bus_output_level / 20.)
        for channel in xrange(num_downmix_channels):
            if logical_bus == solo_bus_index:
                # Solo bus is entirely controlled by PFLs. Notably, independent of muting.
                absLevel = 1. if get_state_param(state_names.channel, dict(channel=channel), 'pfl') else 0
            elif get_state_param(state_names.channel, dict(channel=channel), 'mute'):
                absLevel = 0
            else:
                # Combine the effect of the bus fader with the channel fader to get the gain matrix entry.
                level = get_state_param(state_names.fader, dict(bus=logical_bus, channel=channel), 'lvl')
                absLevel = 10. ** (level/20.) * absBusFaderLevel
            if len(physical_buses) == 1:
                # Mono.
                gain_for_physical_bus[physical_buses[0], channel] = absLevel
            else:
                # Stereo: compute panning (0 = center).
                pan = get_state_param(state_names.fader, dict(bus=logical_bus, channel=channel), 'pan')
                left, right = physical_buses
                gain_for_physical_bus[left , channel] = absLevel * (.5 - pan) ** panning_exponent
                gain_for_physical_bus[right, channel] = absLevel * (.5 + pan) ** panning_exponent

    for bus, downmix in enumerate(mixer.downmixes):
        gain_addresses = downmix.gain[0] #  FIXME: hardcoded core 0
        for channel, gain_addr in enumerate(gain_addresses):
            set_memory(
                core=0,  # hardcoded, until we can test multi-core and get the right abstraction.
                addr=gain_addr,
                data=[gain_for_physical_bus[bus, channel]])


    for core in xrange(HARDWARE_PARAMS['num_cores']):
        # Set constants.
        set_memory(
            core=core,
            addr=constants.base,
            data=constants.constants)

        # Special metering filter.
    set_memory(core=core, addr=meter_filter_param_base, data=StateVarFilter.encode_params(**METERING_LPF_PARAMS))




class Controller(object):
    def __init__(self, io_thread, snapshot_base_dir='snapshots'):
        self.io_thread = io_thread

        self.snapshot_base_dir = snapshot_base_dir
        if not os.path.exists(self.snapshot_base_dir):
            os.makedirs(self.snapshot_base_dir)

        self.state = get_initial_state(metadata)
        self.memoizer = OneStepMemoizer()

        try:
            self.load_snapshot()
            print 'Snapshot loaded.'
        except IOError:
            print 'No snapshot found.'
        except InvalidSnapshot:
            print "Not loading an initial snapshot because it's invalid."

    def load_snapshot(self, name='latest'):
        with open(os.path.join(self.snapshot_base_dir, name), 'rb') as f:
            state = json.load(f)
            if state['metadata'] != self.state['metadata']:
                raise InvalidSnapshot
            self.state.update(state)
        # You probably want to dump_state_to_mixer now.

    def save_snapshot(self):
        now = datetime.now().isoformat()
        filename = os.path.join(self.snapshot_base_dir, now)
        with open(filename, 'wb') as f:
            json.dump(self.state, f)
        new_symlink_name = os.path.join(self.snapshot_base_dir, 'latest-next')
        latest_symlink_name = os.path.join(self.snapshot_base_dir, 'latest')
        if os.path.exists(new_symlink_name):
            os.unlink(new_symlink_name)
        os.symlink(now, new_symlink_name)
        os.rename(new_symlink_name, latest_symlink_name)

    def apply_update(self, control, value):
        """
        Apply a state update.

        Returns True iff the update was handled successfully.
        """
        if control not in self.state:
            return False
        self.state[control] = value
        self._update_state()
        return True

    def get_meter(self):
        raw = self.io_thread.get_meter()[1]
        return dict(
            c=raw[:metadata['num_channels']].tolist(),
            b=raw[metadata['num_channels']:].tolist())

    def dump_state_to_mixer(self):
        self._update_state()

    def _update_state(self):
        desired_param_mem = self.io_thread.desired_param_mem
        def set_memory(core, addr, data):
            desired_param_mem[int(addr):int(addr)+len(data)] = data
        logical_to_physical(self.state, mixer, set_memory, self.memoizer)
        self.memoizer.advance()


class DummyController(Controller):
    def __init__(self, *a, **kw):
        super(DummyController, self).__init__(*a, **kw)
        self.meter_levels = np.zeros(metadata['num_channels'])

    def get_meter(self):
        bus = 0
        core = 0
        cur_gains = [self.io_thread._param_mem_contents[addr] + 1e-6 for addr in mixer.downmixes[bus].gain[core]]
        meter_levels = 20 * np.log10(np.array(cur_gains))
        offsets = np.array([np.sin(2*np.pi*(time.time() + chan / 4.)) for chan in xrange(metadata['num_channels'])])
        return dict(
            c=(meter_levels + offsets).tolist(),
            b=[np.logaddexp.reduce(meter_levels).tolist()]*HARDWARE_PARAMS['num_busses_per_core'])



class DummySPIChannel(object):
    buf_size_in_words = SPI_BUF_SIZE_IN_WORDS

    def transfer(self, **kw):
        import time
        time.sleep(.1)


import os
SPI_DEVICE = '/dev/spidev4.0'
ON_TGT_HARDWARE = os.path.exists(SPI_DEVICE)
if ON_TGT_HARDWARE:
    import spidev
    spi_dev = spidev.SpiChannel(SPI_DEVICE, bits_per_word=20)
    spi_channel = SPIChannel(spi_dev, buf_size_in_words=SPI_BUF_SIZE_IN_WORDS)
    controller_class = Controller
else:
    spi_channel = DummySPIChannel()
    controller_class = DummyController

io_thread = IOThread(param_mem_size=1024, spi_channel=spi_channel)
controller = controller_class(io_thread)
controller.dump_state_to_mixer()

io_thread.start()
