# Cockpit Python bridge channel: websocket-stream1
#
# Provides backward-compatible behavior with the C bridge's
# cockpitwebsocketstream: connects to a remote WebSocket and forwards
# raw frames both directions.

import asyncio
import contextlib
import logging
from typing import Optional

try:
    from aiohttp import (
        ClientSession,
        ClientConnectorError,
        WSServerHandshakeError,
        WSMsgType,
        WSCloseCode,
        ClientWebSocketResponse,
    )
except ImportError as exc:
    raise RuntimeError("websocket-stream1 requires aiohttp to be available") from exc

from ..channel import AsyncChannel, ChannelError
from ..jsonutil import JsonObject, get_str, get_int, get_object

logger = logging.getLogger(__name__)


class WebSocketStream1(AsyncChannel):
    payload = "websocket-stream1"

    """
    A Cockpit channel that represents a WebSocket client.
    Options:
      payload: "websocket-stream1"
      address: string (required)   # host/IP of the target WS server
      port:    int (required)      # port number
      path:    string (required)   # must start with "/"
      tls:     bool (optional)     # if true => wss, else ws (default False)
      headers: dict (optional)     # extra HTTP headers to send on handshake
      protocols: list[str] (optional)  # Sec-WebSocket-Protocol values
      binary:  bool (optional)     # if present, send data as binary frames (default text)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ws: Optional[ClientWebSocketResponse] = None
        self._closed = False
        self._last_error_code = 0

    async def run(self, options: JsonObject) -> None:
        logger.debug("open %s", options)

        address = get_str(options, "address", None)
        port = get_int(options, "port", None)
        path = get_str(options, "path", None)
        tls = options.get("tls", False)
        headers = get_object(options, "headers", lambda d: {k: str(v) for k, v in d.items()}, None)
        protocols = options.get("protocols")
        binary_mode = "binary" in options

        if not address:
            raise ChannelError("protocol-error", message='"address" option is required')
        if port is None:
            raise ChannelError("protocol-error", message='"port" option is required')
        if not path or not path.startswith("/"):
            raise ChannelError("protocol-error", message='invalid or missing "path" field in WebSocket stream request')

        if headers is not None and not isinstance(headers, dict):
            raise ChannelError("protocol-error", message='invalid "headers" field in WebSocket stream request')
        
        if protocols is not None and (not isinstance(protocols, list) or not all(isinstance(p, str) for p in protocols)):
            raise ChannelError("protocol-error", message='invalid "protocols" value in WebSocket stream request')

        scheme = "wss" if tls else "ws"
        url = f"{scheme}://{address}:{port}{path}"
        origin = f"{'https' if tls else 'http'}://{address}"

        logger.debug("websocket-stream1: connecting to %s", url)

        if self._closed:
            return

        session: Optional[ClientSession] = None
        ws: Optional[ClientWebSocketResponse] = None
        reader_task: Optional[asyncio.Task] = None

        try:
            session = ClientSession()
            ws = await session.ws_connect(
                url,
                headers=headers or {},
                protocols=list(protocols) if protocols else None,
                origin=origin,
                autoping=True,
                heartbeat=30.0,
                max_msg_size=0,
            )
            logger.debug("websocket-stream1: connected to %s", url)
            
            self._ws = ws

            response_headers = {}
            try:
                if hasattr(ws, '_response') and ws._response and hasattr(ws._response, 'headers'):
                    for name, value in ws._response.headers.items():
                        response_headers[name] = value
            except Exception:
                # If  can't get headers, send empty
                pass
            
            self.send_control(command="response", headers=response_headers)
            self.ready()

            reader_task = asyncio.create_task(self._read_websocket(ws, binary_mode))

            pending_sends = []
            max_pending = 16 
            
            while True:
                pending_sends = [task for task in pending_sends if not task.done()]
                
                if len(pending_sends) >= max_pending:
                    if pending_sends:
                        await asyncio.wait(pending_sends, return_when=asyncio.FIRST_COMPLETED)
                    continue
                
                data = await self.read()
                if data is None:
                    break
                    
                if ws.closed:
                    logger.debug("websocket-stream1: WebSocket closed, stopping send loop")
                    break
                    
                try:
                    if binary_mode:
                        send_task = asyncio.create_task(ws.send_bytes(data))
                    else:
                        text = data.decode("utf-8")
                        send_task = asyncio.create_task(ws.send_str(text))
                    pending_sends.append(send_task)
                except Exception as exc:
                    logger.debug("websocket-stream1: send error: %s", exc)
                    break
            
            if pending_sends:
                await asyncio.gather(*pending_sends, return_exceptions=True)

        except ClientConnectorError as exc:
            raise ChannelError("not-found", message=f"websocket-stream1: connect failed: {exc}") from exc
        except WSServerHandshakeError as exc:
            raise ChannelError("protocol-error", message=f"websocket-stream1: handshake failed: {exc}") from exc
        except Exception as exc:
            raise ChannelError("internal-error", message=f"websocket-stream1: error: {exc}") from exc
        finally:
            if reader_task:
                reader_task.cancel()
                with contextlib.suppress(Exception):
                    await reader_task
            if ws and not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()
            if session:
                with contextlib.suppress(Exception):
                    await session.close()

        self.done()

    def control(self, command: str, options: JsonObject) -> bool:
        if command == "done":
            ws = self._ws
            if ws and not ws.closed:
                asyncio.create_task(ws.close(code=WSCloseCode.OK, message="disconnected"))
            return True
        return super().control(command, options)

    def close(self, problem: Optional[str] = None) -> None:
        self._closed = True
        ws = self._ws
        if ws and not ws.closed:
            if problem:
                asyncio.create_task(ws.close(code=WSCloseCode.INTERNAL_ERROR, message=problem))
            else:
                asyncio.create_task(ws.close(code=WSCloseCode.OK, message="disconnected"))
        super().close(problem)

    async def _read_websocket(self, ws: ClientWebSocketResponse, binary_mode: bool) -> None:
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = msg.data.encode("utf-8")
                    await self.write(data)
                elif msg.type == WSMsgType.BINARY:
                    await self.write(msg.data)
                elif msg.type in (WSMsgType.CLOSING, WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    if msg.type == WSMsgType.CLOSING:
                        self.send_control(command="done")
                    elif msg.type == WSMsgType.CLOSE:
                        close_code = getattr(ws, 'close_code', None) or self._last_error_code
                        problem = self._map_close_code_to_problem(close_code)
                        self.close(problem)
                    elif msg.type == WSMsgType.ERROR:
                        self._last_error_code = getattr(msg, 'code', 0)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("websocket-stream1: reader error: %s", exc)

    def _map_close_code_to_problem(self, code: Optional[int]) -> Optional[str]:
        if code in (WSCloseCode.OK, WSCloseCode.GOING_AWAY):
            return None
        if code in (0, WSCloseCode.NO_STATUS, WSCloseCode.ABNORMAL_CLOSURE):
            return "disconnected"
        if code in (
            WSCloseCode.PROTOCOL_ERROR,
            WSCloseCode.UNSUPPORTED_DATA,
            WSCloseCode.INVALID_TEXT,
            WSCloseCode.POLICY_VIOLATION,
            WSCloseCode.MESSAGE_TOO_BIG,
            WSCloseCode.TLS_HANDSHAKE,
        ):
            return "protocol-error"
        if code == WSCloseCode.MANDATORY_EXTENSION:
            return "unsupported"
        return "internal-error"