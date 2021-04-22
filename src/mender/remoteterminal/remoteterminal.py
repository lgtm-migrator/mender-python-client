# Copyright 2020 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import logging
import asyncio
import os
import pty
import select
import ssl
import subprocess
import threading
#import signal

import msgpack
import websockets

log = logging.getLogger(__name__)


class RemoteTerminal:
    """ This class serves the RemoteTerminal aka remote shell feature
        over the WebSocket. This supposed to be the only instance and
        serves single or many concurrent connections (next release?)
    """

    def __init__(self):
        log.debug("RemoteTerminal initialized")

        self._client = None
        self._sid = None
        self._ws_connected = False
        self._hello_failed = False
        self._context = None
        # started by a protocol msg "new", ended (set back to False) after a protocol msg "end"
        # @fixme
        self._session_started = False
        self._ext_headers = None
        self._ssl_context = None
        self._master = None
        self._slave = None
        self._shell = None

    async def ws_connect(self):
        # from context we receive sth like: "ServerURL": "https://docker.mender.io"
        # we need replace the protcol and API entry point to achive like:
        # "wss://docker.mender.io/api/devices/v1/deviceconnect/connect"
        uri = self._context.config.ServerURL.replace(
            "https", "wss") + "/api/devices/v1/deviceconnect/connect"
        try:
            self._client = await websockets.connect(
                uri, ssl=self._ssl_context, extra_headers=self._ext_headers)
            self._ws_connected = True
            log.debug(f'connected: {self._client}')
        except Exception as inst:
            log.debug(f'ws_connect: {type(inst)}')
            log.debug(f'ws_connect: {inst}')

    async def ws_listener_background(self):
        await self.ws_connect()
        if self._client is None:
            self._hello_failed = True
            log.debug('Websocket connection failed')
            return -1
        log.debug('Websocket connected')
        try:
            while True:
                log.debug('about to waiting for msg from backend')
                packed_msg = await self._client.recv()
                msg: dict = msgpack.unpackb(packed_msg, raw=False)
                hdr = msg['hdr']
                if hdr['typ'] == 'new' and not self._session_started:
                    self._sid = hdr['sid']
                    self._session_started = True
                    self._open_terminal()
                    self._thread_transmit()
                    # self.thread_read.start()
                if hdr['typ'] == 'shell':
                    self.ws_read_from_backend_write_to_terminal(msg)
                if hdr['typ'] == 'stop':
                    log.debug('close terminal msg')
                    # os.killpg(os.getpgid(self._shell.pid), signal.SIGTERM)
                    self._shell.kill()
                    self._master = None
                    self._slave = None
                    self._session_started = False
                    # self.thread_send.join()

        except Exception as inst:
            log.error(f'hello: {type(inst)}')
            log.error(f'hello: {inst}')

    async def ws_send_terminal_stdout_to_backend(self):
        if self._hello_failed:
            log.debug('leaving ws_send_terminal_stdout_to_backend')
            return -1
        log.debug('going into clnt->bcknd loop')
        while True:
            try:
                data = os.read(self._master, 102400)
                resp_header = {'proto': 1, 'typ': 'shell', 'sid': self._sid}
                resp_props = {'status': 1}
                response = {'hdr': resp_header,
                            'props': resp_props, 'body': data}
                # log.debug(f'resp: {response}')
                await self._client.send(msgpack.packb(response, use_bin_type=True))
                log.debug('data sent')
            except Exception as ex_instance:
                pass    # @fixme those execption are catched all the time due to non-blocking,
                # commented out for keeping the output tidier for testing
                # log.debug(f'send_stdout: {type(ex_instance)}')
                # log.debug(f'send_stdout: {ex_instance}')

    def ws_read_from_backend_write_to_terminal(self, msg):
        log.debug('waiting for _master for writing')
        _, ready, _, = select.select([], [self._master], [])
        for stream in ready:
            log.debug('stream in _master READY for WRITING')
            try:
                os.write(stream, msg['body'])
                # os.write(stream, 'ls\n'.encode('utf-8'))
            except Exception as ex_instance:
                log.error(
                    f'while writing to master: {type(ex_instance)}')
                log.error(
                    f'while writing to master: {ex_instance}')
            except Exception as inst:
                log.error(f'hello: {type(inst)}')
                log.error(f'hello: {inst}')

    #def thread_f_recieve(self):
    #    try:
    #        asyncio.run(self.ws_read_from_backend_write_to_terminal())
    #    except Exception as inst:
    #        log.debug(f'in thread_f_recieve: {type(inst)}')
    #        log.debug(f'in thread_f_recieve: {inst}')

    def thread_f_transmit(self):
        try:
            asyncio.run(self.ws_send_terminal_stdout_to_backend())
        except Exception as inst:
            log.error(f'in thread_f_transmit: {type(inst)}')
            log.error(f'in thread_f_transmit: {inst}')

    def thread_f_ws(self):
        try:
            asyncio.run(self.ws_listener_background())
        except Exception as inst:
            log.error(f'in thread_f_transmit: {type(inst)}')
            log.error(f'in thread_f_transmit: {inst}')

    #def _thread_recieve(self):
    #    log.debug('about to start read thread')
    #    thread_read = threading.Thread(target=self.thread_f_recieve)
    #    thread_read.start()

    def _thread_transmit(self):
        log.debug('about to start send thread')
        thread_send = threading.Thread(target=self.thread_f_transmit)
        thread_send.start()

    def _background_thread_ws(self):
        log.debug('about to start naver ending thread, websocket connection')
        thread_ws = threading.Thread(target=self.thread_f_ws)
        thread_ws.start()

    def _open_terminal(self):
        user = 'nobody'
        if user := self._context.remoteTerminalConfig.User:
            pass
        self._master, self._slave = pty.openpty()
        self._shell = subprocess.Popen(
            [self._context.config.ShellCommand, "-i"],
            start_new_session=True,
            stdin=self._slave,
            stdout=self._slave,
            stderr=self._slave,
            user=self._context.remoteTerminalConfig.User)
        log.debug(f"Open terminal as: {user}")

    def run(self, context):
        self._context = context
        if context.remoteTerminalConfig.RemoteTerminal and context.authorized and not self._ws_connected:
            log.debug(f'_ws_connected={self._ws_connected}')
            self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

            # @fixme: check if the file exists, and check if entry exist as the cert
            # may be taken from ca-certificates
            if context.config.ServerCertificate:
                self._ssl_context.load_verify_locations(
                    context.config.ServerCertificate)
            else:
                self._ssl_context = ssl.create_default_context()

            # the JWT should already be acquired as we supposed to be in AuthorizedState
            self._ext_headers = {
                'Authorization': 'Bearer ' + context.JWT
            }

            self._background_thread_ws()
            log.debug("i've just invoked the websocket thread")
