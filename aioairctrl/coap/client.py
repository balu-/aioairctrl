import json
import logging
import os
import asyncio
from collections.abc import Callable

from aioairctrl.coap import aiocoap_monkeypatch  # noqa: F401
from aiocoap import (
    Context,
    GET,
    Message,
    NON,
    POST,
)

from aioairctrl.coap.encryption import EncryptionContext

logger = logging.getLogger(__name__)

class Timer:
    def __init__(self, timeout, callback):
        self._softRestart = False
        self._timeout = timeout
        self._callback = callback
        self._task = asyncio.ensure_future(self._job())

    async def _job(self):
        await asyncio.sleep(self._timeout)
        while self._softRestart == True:
            self._softRestart = False
            await asyncio.sleep(self._timeout)
        logger.info("Timeout!")
        await self._callback()

    def cancel(self):
        self._task.cancel()

    def soft_restart(self):
        self._softRestart = True

    def setTimeout(self, timeout:int):
        self._timeout = timeout



class Client:
    STATUS_PATH = "/sys/dev/status"
    CONTROL_PATH = "/sys/dev/control"
    SYNC_PATH = "/sys/dev/sync"

    def __init__(self, host, port=5683):
        self.host = host
        self.port = port
        self._client_context = None
        self._encryption_context = None

    async def _init(self):
        self._client_context = await Context.create_client_context()
        self._encryption_context = EncryptionContext()
        await self._sync()

    @classmethod
    async def create(cls, *args, **kwargs):
        obj = cls(*args, **kwargs)
        await obj._init()
        return obj

    async def shutdown(self) -> None:
        if self._client_context:
            await self._client_context.shutdown()

    async def _sync(self):
        logger.debug("syncing")
        sync_request = os.urandom(4).hex().upper()
        request = Message(
            code=POST,
            mtype=NON,
            uri=f"coap://{self.host}:{self.port}{self.SYNC_PATH}",
            payload=sync_request.encode(),
        )
        response = await self._client_context.request(request).response
        client_key = response.payload.decode()
        logger.debug("synced: %s", client_key)
        self._encryption_context.set_client_key(client_key)

    async def get_status(self):
        logger.debug("retrieving status")
        request = Message(
            code=GET,
            mtype=NON,
            uri=f"coap://{self.host}:{self.port}{self.STATUS_PATH}",
        )
        #request.opt.observe = 0 # no observe here
        response = await self._client_context.request(request).response
        payload_encrypted = response.payload.decode()
        payload = self._encryption_context.decrypt(payload_encrypted)
        logger.debug("status: %s", payload)
        state_reported = json.loads(payload)
        return state_reported["state"]["reported"]

    async def observe_status(self, on_valuechange_callback: Callable[[dict], None] ):
        """ Observe status call on_valuechange_callback when data changes"""
        def decrypt_status(response):
            payload_encrypted = response.payload.decode()
            payload = self._encryption_context.decrypt(payload_encrypted)
            logger.debug("observation status: %s", payload)
            status = json.loads(payload)
            return status["state"]["reported"]

        def setFuture(observation_is_over, value):
            logger.info("Setting Future value")
            observation_is_over.set_result(value)

        logger.debug("observing status")
        request = Message(
            code=GET,
            mtype=NON,
            uri=f"coap://{self.host}:{self.port}{self.STATUS_PATH}",
        )
        request.opt.observe = 0
        requester = self._client_context.request(request)
        ## https://github.com/chrysn/aiocoap/blob/1f03d4ceb969b2b443c288c312d44c3b7c3e2031/aiocoap/cli/client.py#L296
        # preset timeout
        timeout = 100
        logger.info("Callback part start")
        observation_is_over = asyncio.get_event_loop().create_future()
        
        timer = Timer(timeout, lambda observation_is_over=observation_is_over, value="Timeout": setFuture(observation_is_over, value))  # set timer to cancel listening
        #requester.observation.register_errback(observation_is_over.set_result)
        requester.observation.register_errback(lambda observation_is_over=observation_is_over, value="Error": self.setFuture(observation_is_over, value))
        requester.observation.register_callback(lambda data, timeout=timeout: ( timer.soft_restart(), on_valuechange_callback(decrypt_status(data))))#lambda data, options=options: incoming_observation(options, data))
        
        logger.info("Get first data")
        response = await requester.response
        logger.info(f"max age {response.opt.max_age}")
        timeout = response.opt.max_age + 7 # add some slack
        timer.setTimeout(timeout)

        data = decrypt_status(response)
        logger.info(f"Decrypted {data} - call callback")
        on_valuechange_callback(data)

        exit_reason = await observation_is_over
        logger.error("Observation is over: %r"%(exit_reason,))#, file=sys.stderr)
        ####
        
        #async for response in requester.observation:
        #    yield decrypt_status(response)

    async def set_control_value(self, key, value, retry_count=5, resync=True) -> None:
        return await self.set_control_values(
            data={key: value}, retry_count=retry_count, resync=resync
        )

    async def set_control_values(self, data: dict, retry_count=5, resync=True) -> None:
        state_desired = {
            "state": {
                "desired": {
                    "CommandType": "app",
                    "DeviceId": "",
                    "EnduserId": "",
                    **data,
                }
            }
        }
        payload = json.dumps(state_desired)
        logger.debug("REQUEST: %s", payload)
        payload_encrypted = self._encryption_context.encrypt(payload)
        request = Message(
            code=POST,
            mtype=NON,
            uri=f"coap://{self.host}:{self.port}{self.CONTROL_PATH}",
            payload=payload_encrypted.encode(),
        )
        response = await self._client_context.request(request).response
        logger.debug("RESPONSE: %s", response.payload)
        result = json.loads(response.payload)
        if result.get("status") == "success":
            return True
        else:
            if resync:
                logger.debug("set_control_value failed. resyncing...")
                await self._sync()
            if retry_count > 0:
                logger.debug("set_control_value failed. retrying...")
                return await self.set_control_values(data, retry_count - 1, resync)
            logger.error("set_control_value failed: %s", data)
            return False
