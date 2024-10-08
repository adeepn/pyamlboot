#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0 OR MIT
# -*- coding: utf-8 -*-

__license__ = "GPL-2.0"
__author__ = "Arseniy Krasnov"
__email__ = "avkrasnov@salutedevices.com"
__copyright__ = "Copyright (c) 2024, SaluteDevices"
__version__ = '0.0.1'

import logging
import time

from enum import IntEnum
from types import DynamicClassAttribute

import usb.core
import usb.util

ADNL_REPLY_OKAY = 'OKAY'
ADNL_REPLY_FAIL = 'FAIL'
ADNL_REPLY_INFO = 'INFO'
ADNL_REPLY_DATA = 'DATA'

USB_IO_TIMEOUT_MS = 5000
USB_BULK_SIZE = 16384
USB_READ_LEN = 512

AMLOGIC_VENDOR_ID = 0x1b8e
AMLOGIC_PRODUCT_ID = 0xc004

BOOTROM_BURNSTEPS_0 = 0xC0040000
BOOTROM_BURNSTEPS_1 = 0xC0040001
BOOTROM_BURNSTEPS_2 = 0xC0040002
BOOTROM_BURNSTEPS_3 = 0xC0040003

TPL_BURNSTEPS_0 = 0xC0041030
TPL_BURNSTEPS_1 = 0xC0041031
TPL_BURNSTEPS_2 = 0xC0041032


class Stage(IntEnum):
    """
    Enum with boot stages of Amlogic SoC.
    """
    ROM = 0
    SPL = 8
    TPL = 16

    @DynamicClassAttribute
    def name(self):
        """
        Method provides custom mapping between Enum member and
        user-friendly name of the boot stage in terms of Amlogic.
        """
        name = {
            self.ROM: "BootROM",
            self.SPL: "BL2",
            self.TPL: "U-Boot",
        }.get(self)

        if name:
            return name

        # If self (i.e. Enum member) is not found in the dict, then
        # get() would return None by default. Hence return default name
        # of Enum member in such case.
        return super().name


class SocFamily(IntEnum):
    """
    Map of SoC Family name to numeric ID.
    """
    A1 = 0x2c
    C1 = 0x30
    SC2 = 0x32
    C2 = 0x33
    T5 = 0x34
    T5D = 0x35
    T7 = 0x36
    S4 = 0x37


class FeatSecurebootMask(IntEnum):
    """
    Defines secureboot bit inside FEAT for different SoCs.
    """
    A1 = 0x1
    C1 = 0x1
    C2 = 0x1
    T5 = 0x10
    T5D = 0x10


class CBW:
    def __init__(self, msg) -> None:
        magic = msg[4:8].tobytes().decode()

        if magic != 'AMLC':
            raise RuntimeError('Unexpected CBW magic')

        self._seq = int.from_bytes(msg[8:12], 'little')
        self._size = int.from_bytes(msg[12:16], 'little')
        self._offs = int.from_bytes(msg[16:20], 'little')
        self._need_checksum = not int.from_bytes(msg[20:21], 'little')
        self._end = msg[21]

    def size(self):
        return self._size

    def done(self):
        return self._end

    def offset(self):
        return self._offs


def adnl_get_prefix(msg):
    return msg[:4].tobytes().decode()


def adnl_checksum(buf):
    # This checksum is used to verify part of image, transmitted by
    # USB. It is not for verifying entire partition image.
    return sum(int.from_bytes(buf[i:i + 4], 'little')
               for i in range(0, len(buf), 4)) & 0xffffffff


def send_cmd(epout, epin, cmd, expected_res=ADNL_REPLY_OKAY):
    '''
    Any command reply looks like:
        | header (4b) | payload |, where
    * header is a kind of ack for cmd, with values: OKAY, FAIL, INFO or DATA
    * payload depends on the cmd type
    '''
    epout.write(cmd, USB_IO_TIMEOUT_MS)
    msg = epin.read(USB_READ_LEN, USB_IO_TIMEOUT_MS)

    if len(msg) < 4:
        raise RuntimeError(f'Too short reply: {len(msg)}')

    header = adnl_get_prefix(msg)

    if header != expected_res:
        raise RuntimeError(f'Unexpected reply:{header} to cmd:{cmd}')

    return msg


def send_cmd_identify(epout, epin):
    '''
    Identify command reply:
    * msg[4]  - protocol type: 5 - ADNL, 3 - Optimus
    * msg[5]  - minor version
    * msg[7]  - stage of bootstrap, see stages dict
    * msg[11] - pages map
    '''
    msg = send_cmd(epout, epin, 'getvar:identify')
    # extra checks for this type of command
    if msg[4] != 0x5:
        raise RuntimeError('Unexpected data in reply to "identify"')

    return Stage(msg[7])


def get_chipinfo(epout, epin, page, offset=None, nbytes=None):
    """
    Chipinfo is an information, consisting of several 'pages' with the size of
    64 bytes. First 4 bytes represent particular 'page', e.g.:
      * chipinfo-0: is index page (00: 58444E49 ... | 'X D N I')
      * chipinfo-1: is chip page  (00: 50494843 ... | 'P I H C')
      * chipinfo-3: is ROM page   (00: 564D4F52 ... | 'V M O R')

    By default, function returns whole 'page', otherwise - the requested
    'nbytes' from specified 'offset' inside the 'page'.
    """
    stage = send_cmd_identify(epout, epin)
    if stage not in (Stage.ROM, Stage.SPL):
        raise RuntimeError(f"chipinfo-{page} can't be queried from "
                           f"stage:{stage.name}. Programmer error.")

    if not 0 <= page <= 7:
        raise RuntimeError(f"page index:{page} is out of range [0, 7]")

    # Cut off header of reply msg
    msg = send_cmd(epout, epin, f"getvar:getchipinfo-{page}")[4:]
    if offset is None:
        offset = 0
    if nbytes is None:
        nbytes = msg.buffer_info()[1]
    if offset > 0 and offset + nbytes >= msg.buffer_info()[1]:
        raise RuntimeError("Bad parameters: out of bound access")

    return msg[offset : offset + nbytes]


def adnl_get_feat(epout, epin):
    """
    FEAT is 4 bytes value, residing in 'chipinfo-1'-page. Sometimes FEAT is
    printed out by bootROM or SPL into UART. This value consists of flags.
    """
    feat = get_chipinfo(epout, epin, 1, offset=0x24, nbytes=4)
    return int.from_bytes(feat, "little")


def adnl_get_soc_family_id(epout, epin):
    """
    SoC Family is 4 byte value from 'chipinfo-1'-page.
    """
    soc_fid = get_chipinfo(epout, epin, 1, offset=0x4, nbytes=4)
    return SocFamily(int.from_bytes(soc_fid, "little"))


def is_secureboot_enabled(epout, epin):
    stage = send_cmd_identify(epout, epin)

    if stage != Stage.ROM:
        raise RuntimeError(
            f"Non suitable stage:{stage.name} for 'secureboot' query"
        )

    feat = adnl_get_feat(epout, epin)
    soc_fid = adnl_get_soc_family_id(epout, epin)
    sb_mask = FeatSecurebootMask[soc_fid.name]

    logging.info("SoC family '%s' (%#x): FEAT is %#x, secureboot mask:%#x",
                 soc_fid.name, soc_fid, feat, sb_mask)

    return feat & sb_mask


def send_burnsteps(epout, epin, burnstep):
    send_cmd(epout, epin, 'setvar:burnsteps', ADNL_REPLY_DATA)

    # 'burnsteps' needs extra argument
    send_cmd(epout, epin, burnstep.to_bytes(4, 'little'))

def tpl_burn_partition(part_item, aml_img, epout, epin):
    part_name = part_item.sub_type()
    logging.info('Burning partition "%s"', part_name)
    # To burn partition, first send the following command:
    # 'oem mwrite <partition size> normal store <partition name>'
    # Reply must be 'OKAY'.
    oem_cmd = f'oem mwrite 0x{part_item.size():x} normal store {part_name}'
    send_cmd(epout, epin, oem_cmd)

    while True:
        # Partition is sent step by step, each step starts from
        # command 'mwrite:verify=addsum', reply is 'DATAOUTX:Y'.
        # X is number of bytes, which device expects in this step.
        # Y is offset in the partition image. Both X and Y are in
        # hex format. If reply is 'OKAY' instead of 'DATAOUTX:Y',
        # then current step is done. After both 'X' and 'Y' are
        # received, 'X' number of bytes are sent by blocks, each
        # block is 16KB max. After 'X' bytes are transmitted,
        # control sum packet is sent. It is trimmed by 4 bytes.
        # Valid reply for checksum is 'OKAY'. Step is done, now
        # go to this step again to send rest of partition.
        # After entire parition is sent, checksum verification for
        # just transmitted partition is performed. To do this,
        # client sends 'oem verify sha1sum X', where 'X' is SHA1
        # hash for the whole parition image (currently only SHA1
        # is supported). In case of successful verification,
        # device replies 'OKAY'.

        epout.write('mwrite:verify=addsum', USB_IO_TIMEOUT_MS)
        msg = epin.read(USB_READ_LEN, USB_IO_TIMEOUT_MS)
        strmsg = msg.tobytes().decode()

        if strmsg.startswith(ADNL_REPLY_OKAY):
            logging.info('Burning is done')
            break

        if not strmsg.startswith('DATAOUT'):
            raise RuntimeError(
                f'Unexpected reply to "mwrite:verify=addsum": {strmsg}')

        size_offs = strmsg[7:].split(':')
        size = int(size_offs[0], 16)
        offs = int(size_offs[1], 16)

        part_item.seek(offs)
        buf = part_item.read(size)
        sum_res = adnl_checksum(buf)
        buf_offs = 0

        while size > 0:
            to_send = min(size, USB_BULK_SIZE)
            epout.write(buf[buf_offs:buf_offs + to_send], USB_IO_TIMEOUT_MS)
            size -= to_send
            buf_offs += to_send

        bytes_sum = [(sum_res >> i) & 0xff for i in range(0, 32, 8)]

        try:
            send_cmd(epout, epin, bytes_sum)
        except RuntimeError as e:
            raise RuntimeError('CRC error during tx') from e

    # get 'VERIFY' entry for this partition
    verify_item = aml_img.item_get('VERIFY', part_name)
    sha1sum_str = f'oem verify {verify_item.read().decode("utf-8")}'
    logging.info('Verifying partition checksum using SHA1...')
    epout.write(sha1sum_str)

    strmsg = ''

    while strmsg != ADNL_REPLY_OKAY:
        logging.info('Waiting reply...')
        msg = epin.read(USB_READ_LEN, USB_IO_TIMEOUT_MS)
        strmsg = adnl_get_prefix(msg)

        if strmsg == ADNL_REPLY_INFO:
            # Device is busy because of processing checksum
            time.sleep(1)
            continue

        if strmsg != ADNL_REPLY_OKAY:
            raise RuntimeError('CRC error for partition')

    logging.info('OK')


def send_and_handle_cbw(epout, epin):
    # CBW seems to be Control Block Word. This structure is used
    # during SPL stage. Devices requests blocks of TPL image
    # using this structure.
    logging.info('Requesting CBW...')
    msg = send_cmd(epout, epin, 'getvar:cbw')

    return CBW(msg)


def run_bootrom_stage(epout, epin, aml_img, has_secureboot):
    (sub_type, boot_type) = (
        ("DDR_ENC", "secure") if has_secureboot else ("DDR", "normal")
    )

    logging.info("Device's %s boot is in progress...", boot_type)

    item = aml_img.item_get('USB', sub_type)

    logging.info('Running ROM stage...')
    # May be, sequence of commands below is not necessary,
    # but as from the device side there is closed ROM code
    # which replies to this commands, let's follow with these
    # commands as in vendor's code.
    send_cmd(epout, epin, 'getvar:serialno')
    send_cmd(epout, epin, 'getvar:getchipinfo-1')
    send_cmd(epout, epin, 'getvar:getchipinfo-0')
    send_cmd(epout, epin, 'getvar:getchipinfo-1')
    send_cmd(epout, epin, 'getvar:getchipinfo-2')
    send_cmd(epout, epin, 'getvar:getchipinfo-3')
    send_burnsteps(epout, epin, BOOTROM_BURNSTEPS_0)
    send_cmd(epout, epin, 'getvar:getchipinfo-1')
    send_burnsteps(epout, epin, BOOTROM_BURNSTEPS_1)

    # Preparing to send SPL (e.g. BL2)
    msg = send_cmd(epout, epin, 'getvar:downloadsize')

    # Reply is null-terminated
    bl2_size = int(msg.tobytes().split(b'\x00', 1)[0][4:], 0)

    logging.info('Send download size:0x{:08x} for BL2'.format(bl2_size))
    # Despite another size of image, send only part of whole image with the
    # size, extracted by 'downloadsize' cmd - seems ROM code works only with
    # this value.
    send_cmd(epout, epin, 'download:{:08x}'.format(bl2_size), ADNL_REPLY_DATA)

    logging.info('Sending SPL image...')
    send_cmd(epout, epin, item.read())
    logging.info('Done')

    send_burnsteps(epout, epin, BOOTROM_BURNSTEPS_2)

    logging.info('Send boot cmd')
    send_cmd(epout, epin, 'boot')


def run_bl2_stage(epout, epin, aml_img, has_secureboot):
    # This stage writes to sticky register, then sends U-boot image
    # to the device and runs it. U-boot sees value in this sticky reg
    # and enters USB gadget mode to continue ADNL burning process.
    logging.info('Running BL2 stage...')

    # First, check that we are in SPL stage
    stage = send_cmd_identify(epout, epin)
    if stage != Stage.SPL:
        raise RuntimeError(
            f'Stage:{stage.name}. Seems, BL2 has not been booted yet. '
            'Probably, you are trying to boot for example the unsigned '
            'BL2 on securebooted device. Check your image, please'
        )

    logging.info('Send burnsteps after BL2')
    send_burnsteps(epout, epin, BOOTROM_BURNSTEPS_3)

    sub_type = 'UBOOT_ENC' if has_secureboot else 'UBOOT'
    item = aml_img.item_get('USB', sub_type)

    while True:
        # request cbw
        cbw = send_and_handle_cbw(epout, epin)

        size = cbw.size()

        if cbw.done() != 0:
            logging.info('TPL sending is done')
            break

        item.seek(cbw.offset())
        buf = item.read(size)
        buf_offs = 0
        cur_sum = 0

        while size > 0:
            to_send = min(size, USB_BULK_SIZE)
            send_cmd(epout, epin, f'download:{to_send:08x}', ADNL_REPLY_DATA)

            try:
                send_cmd(epout, epin, buf[buf_offs:buf_offs + to_send])
            except RuntimeError as e:
                raise RuntimeError('Data tx failed') from e

            cur_sum += adnl_checksum(buf[buf_offs:buf_offs + to_send])
            size -= to_send
            buf_offs += to_send

        send_cmd(epout, epin, 'setvar:checksum', ADNL_REPLY_DATA)

        bytes_sum = [(cur_sum >> i) & 0xff for i in range(0, 32, 8)]

        try:
            send_cmd(epout, epin, bytes_sum)
        except RuntimeError as e:
            raise RuntimeError('CRC error during tx') from e

        logging.info('Sending CRC done')


def tpl_send_burnsteps(epout, epin, second_arg):
    send_cmd(epout, epin, f'oem setvar burnsteps {hex(second_arg)}')


def get_device_eps(dev):
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    direction = usb.util.endpoint_direction
    # match the first OUT endpoint
    epout = usb.util.find_descriptor(intf, custom_match=lambda e:
                                     direction(e.bEndpointAddress) ==
                                     usb.util.ENDPOINT_OUT)
    # match the first IN endpoint
    epin = usb.util.find_descriptor(intf, custom_match=lambda e:
                                    direction(e.bEndpointAddress) ==
                                    usb.util.ENDPOINT_IN)

    return [epout, epin]


def wait_for_device(last_dev_addr):
    while True:
        dev = usb.core.find(idVendor=AMLOGIC_VENDOR_ID,
                            idProduct=AMLOGIC_PRODUCT_ID, backend=None)

        # Uboot reenables USB on the device and enters gadget mode
        # again, so wait until device appears on the USB bus with
        # another address.
        if dev is not None:
            if dev.address != last_dev_addr:
                logging.info('Device found')
                return dev

        logging.info('Waiting for the device...')
        time.sleep(1)


def run_tpl_stage(reset, erase_code, aml_img, dev_addr_rom_stage):
    # This stage runs, when Uboot is executed on the device.
    # It burns partitions (rom and spl doesn't touch storage)
    # and verifies them.
    logging.info('Running TPL stage...')

    dev = wait_for_device(dev_addr_rom_stage)

    epout, epin = get_device_eps(dev)

    logging.info('Sending identify...')

    send_cmd_identify(epout, epin)

    tpl_send_burnsteps(epout, epin, TPL_BURNSTEPS_0)
    tpl_send_burnsteps(epout, epin, TPL_BURNSTEPS_1)
    send_cmd(epout, epin, f'oem disk_initial {erase_code}')
    tpl_send_burnsteps(epout, epin, TPL_BURNSTEPS_2)

    for item in aml_img.items():
        if item.main_type() == 'PARTITION':
            tpl_burn_partition(item, aml_img, epout, epin)

    if reset:
        logging.info('Reset')
        send_cmd(epout, epin, 'reboot')


def do_adnl_burn(reset, erase_code, aml_img):
    logging.basicConfig(level=logging.INFO,
                        format='[ANDL] %(message)s')
    logging.info('Looking for USB device...')

    try:
        dev = usb.core.find(idVendor=AMLOGIC_VENDOR_ID,
                            idProduct=AMLOGIC_PRODUCT_ID, backend=None)
    except usb.core.NoBackendError:
        logging.error('Please install libusb')
        raise

    if dev is None:
        logging.info('Device not found')
        return

    dev_addr_rom_stage = dev.address

    logging.info('Setting up USB device')
    epout, epin = get_device_eps(dev)

    stage = send_cmd_identify(epout, epin)

    if stage == Stage.TPL:
        send_cmd(epout, epin, 'reboot-romusb')

        dev = wait_for_device(dev_addr_rom_stage)
    elif stage != Stage.ROM:
        raise RuntimeError(f'Unknown stage: {stage.name}')

    dev_addr_rom_stage = dev.address
    epout, epin = get_device_eps(dev)

    has_secureboot = is_secureboot_enabled(epout, epin)

    run_bootrom_stage(epout, epin, aml_img, has_secureboot)
    run_bl2_stage(epout, epin, aml_img, has_secureboot)
    run_tpl_stage(reset, erase_code, aml_img, dev_addr_rom_stage)

    logging.info('Done, amazing!')
