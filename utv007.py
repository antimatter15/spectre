#!/usr/bin/env python
# Copyright (c) 2013 Federico Ruiz Ugalde
# Copyright (c) 2014 Kevin Kwok
# 
# Author: Federico Ruiz-Ugalde <memeruiz at gmail dot com>
# Author: Kevin Kwok <antimatter15@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import usb1 as usb
from protocol import p_init, p5
from protocol import *
from time import sleep
import pygame

class Utv007(object):
    interface = 0

    def __init__(self, device = "/dev/video1"):
        self.cont = usb.USBContext()
        
        easycap_dev_id = '0x1b71:0x3002'
        for i in self.cont.getDeviceList():
            print "ID", i.getVendorID(),i.getProductID(), i.getManufacturer(), "Serial: ", i.getSerialNumber(), "Num conf", i.getNumConfigurations()
            if hex(i.getVendorID()) + ':' + hex(i.getProductID()) == easycap_dev_id:
                print "Easycap utv007 found! Dev ID: ", easycap_dev_id
                self.dev = i
                break
        if self.dev:
            print "Opening device"
            self.devh = self.dev.open()
        else:
            print "No easycap utv007 devices found"
            exit()

        while self.devh.kernelDriverActive(self.interface):
            self.devh.detachKernelDriver(self.interface)
            sleep(0.5)

        print "Claiming interface"
        self.devh.claimInterface(self.interface)
        print "Preinit"
        run_protocol(p_preinit, self.devh)
        print "Init"
        run_protocol(p_init, self.devh)
        run_protocol(p5, self.devh)
        print "Set Altsetting to 1"
        self.devh.setInterfaceAltSetting(self.interface,1)

        #packet related:
        self.expected_toggle     = True
        self.expected_n_s_packet = 0
        self.expected_n_img      = 0
        self.start_frame         = True
        self.n_packets           = 0
        self.stop                = False
        self.iso                 = []
        self.framebuffer         = bytearray(720 * 480 * 2) # 2 bytes per pixel

    def __enter__(self):
        print "Enter"
        return self

    def __exit__(self, type, value, traceback):
        for iso in self.iso:
            try:
                iso.cancel()
            except:
                print "unable to cancel"
        print "Releasing interface"
        self.devh.releaseInterface(0)
        print "Closing device handler"
        self.devh.close()
        print "Exiting context"
        self.cont.exit()
        pass

    def do_iso2(self):
        iso = self.devh.getTransfer(iso_packets=8)
        iso.setIsochronous(0x81, buffer_or_len=0x6000, callback=self.callback2, timeout=1000)
        iso.submit()
        self.iso.append(iso)


    def handle_ev(self):
        self.cont.handleEvents()

    """ 
        buffer_list is a list that contains around 8 packets inside, each of this packets contains 3 smaller packets inside
        The first four bytes of this s_packets are special:
        1) 0x88 always
        2) frame counter
        3) 8bit: toogle frame bit (for interlacing), 7-0bits packet counter
        4) packet counter
        With frame counter one can know if we are loosing frames
        With the packet counter one can know if we have incomplete frames
        With the toogle frame bit it is possible to generate the correct complete progressive image
        This four bytes must be removed from the image data.
        The last 60 bytes are black filled (for synchronization?) and must be removed
        Each s_packet is 1024 long but once we remove this bytes the data payload is 1024-4-60=960 bytes long.
        If packet starts with 0x00 instead of 0x88, it means it is empty and to be ignored

        In this routine we find the start of first of the two interlaced images, and then we start processing
    """
    def build_images(self, buffer_list, setup_list):
        packets = [self.buffer_list[i][:int(self.setup_list[i]['actual_length'])] for i in xrange(len(self.buffer_list))]
        for packet in packets:
            if len(packet) == 0:
                continue

            for s_packet in [packet[:len(packet)/3], packet[len(packet)/3:2*len(packet)/3], packet[2*len(packet)/3:len(packet)]]:
                if ord(s_packet[0]) != 0x88:
                    continue

                n_img      = ord(s_packet[1])
                n_s_packet = ((ord(s_packet[2]) & 0x0f)<< 8) | (ord(s_packet[3]))
                n_toggle   = (((ord(s_packet[2]) & 0xf0) >> 7) == 0)

                n = (n_s_packet + int(not n_toggle) * 360) * 960
                self.framebuffer[n: n + 960] = s_packet[4:1024-60]

                if n_toggle == False and n_s_packet == 359:
                    camclock.tick()
                    # self.framebuffer = bytearray(720 * 480 * 2)


    def callback2(self, transfer):
        self.buffer_list = transfer.getISOBufferList()
        self.setup_list  = transfer.getISOSetupList()
        self.status      = transfer.getStatus()
        self.build_images(self.buffer_list, self.setup_list)

        if not self.stop:
            transfer.submit()

import numpy as np
from PIL import Image
import threading
import signal
from time import strftime
try:
    import cv2
except:
    print "OpenCV 2 Not Available. Video can not be recorded."

quit_now = False
screen   = None
record   = None

renclock = pygame.time.Clock()
camclock = pygame.time.Clock()

def convert_pil(framebuffer):
    yuyv = np.reshape(framebuffer, (480 * 720 * 2 / 4, 4))
    together = np.vstack((yuyv[:,0], yuyv[:,1], yuyv[:,3], yuyv[:,2], yuyv[:,1], yuyv[:,3])).T.reshape((480, 720 * 3))
    # deinterlace
    deinterlaced = np.zeros((480, 720 * 3), dtype='uint8')
    deinterlaced[1::2 ,:] = together[:240,:]
    deinterlaced[::2,:] = together[240:,:] 
    im = Image.frombuffer("YCbCr", (720, 480), deinterlaced.flatten(), 'raw', 'YCbCr', 0, 1).convert('RGB')
    return im

def display_frame(im):
    surface = pygame.image.fromstring(im.tostring(), (720, 480), "RGB")
    screen.blit(surface, (0,0)) 
    
    font = pygame.font.Font(None, 36)
    text = font.render("FPS: %1.1f" % (camclock.get_fps()), 1, (190, 10, 10))
    screen.blit(text, (10, 10))
    if record is not None:
        text = font.render("Recording", 1, (190, 10, 10))
        screen.blit(text, (590, 450))
    # print "fps", renclock.get_fps()
    pygame.display.flip()
    renclock.tick()


# https://www.mail-archive.com/fx2lib-devel@lists.sourceforge.net/msg00048.html
# http://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread-in-python
class ListenThread(threading.Thread):
    def __init__(self, utv):
        self.utv = utv 
        threading.Thread.__init__(self)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        for i in xrange(20):
            self.utv.do_iso2()
        while not self._stop.isSet():
            self.utv.handle_ev()

def signal_handler(signal, frame):
    global quit_now
    quit_now = True

def main():
    signal.signal(signal.SIGINT, signal_handler)
    pygame.init()
    global screen, quit_now, record
    size = (720, 480)
    screen = pygame.display.set_mode(size)
    pygame.display.set_caption("Fushicai EasyCAP utv007")

    with Utv007() as utv:
        lt = ListenThread(utv)
        lt.start()

        while not quit_now:
            im = convert_pil(utv.framebuffer)
            display_frame(im)

            if record is not None and record.isOpened():
                # maybe we should use opencv/highgui instead of pygame
                imcv = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
                # cv2.imshow('frame', imcv)
                record.write(imcv)
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    quit_now = True
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_r:
                        if record is None:
                            # http://stackoverflow.com/questions/10605163/opencv-videowriter-under-osx-producing-no-output
                            fourcc = cv2.cv.CV_FOURCC('m','p','4','v')
                            filename = strftime("Recording %Y-%m-%d %H.%M.%S.mov")
                            record = cv2.VideoWriter(filename, fourcc, 20.0, (720, 480))
                        else:
                            print "finishing up the recording"
                            record.release()
                            record = None
                    elif event.key == pygame.K_SPACE:
                        screen.fill((255, 255, 255)) # flash the screen because its a snapshot
                        pygame.display.flip()
                        filename = strftime("Snapshot %Y-%m-%d %H.%M.%S.jpg")
                        im.save(filename)
                        print "Saving snapshot as %s" % filename

        utv.stop = True
        lt.stop()
        if record is not None:
            record.release()
        # exit()

if __name__=="__main__":
    main()