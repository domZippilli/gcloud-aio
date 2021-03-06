import binascii
import enum
import io
import json
import logging
import mimetypes
import os
import sys
from typing import Any
from typing import AnyStr
from typing import Dict
from typing import IO
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union
from urllib.parse import quote

from gcloud.aio.auth import AioSession  # pylint: disable=no-name-in-module
from gcloud.aio.auth import BUILD_GCLOUD_REST  # pylint: disable=no-name-in-module
from gcloud.aio.auth import Token  # pylint: disable=no-name-in-module
from gcloud.aio.storage.bucket import Bucket

# Selectively load libraries based on the package
if BUILD_GCLOUD_REST:
    from time import sleep
    from requests import HTTPError as ResponseError
    from requests import Session
else:
    from asyncio import sleep  # type: ignore[misc]
    from aiohttp import ClientResponseError as ResponseError  # type: ignore[no-redef]  # pylint: disable=line-too-long
    from aiohttp import ClientSession as Session  # type: ignore[no-redef]


API_ROOT = 'https://www.googleapis.com/storage/v1/b'
API_ROOT_UPLOAD = 'https://www.googleapis.com/upload/storage/v1/b'
VERIFY_SSL = True
SCOPES = [
    'https://www.googleapis.com/auth/devstorage.read_write',
]

MAX_CONTENT_LENGTH_SIMPLE_UPLOAD = 5 * 1024 * 1024  # 5 MB
WRITE_BUFFER_SIZE = 4096


STORAGE_EMULATOR_HOST = os.environ.get('STORAGE_EMULATOR_HOST')
if STORAGE_EMULATOR_HOST:
    API_ROOT = f'https://{STORAGE_EMULATOR_HOST}/storage/v1/b'
    API_ROOT_UPLOAD = f'https://{STORAGE_EMULATOR_HOST}/upload/storage/v1/b'
    VERIFY_SSL = False


log = logging.getLogger(__name__)


def choose_boundary() -> str:
    """Stolen from urllib3.filepost.choose_boundary() as of v1.26.2."""
    boundary = binascii.hexlify(os.urandom(16))
    if sys.version_info.major == 2:
        return boundary  # type: ignore[return-value]
    return boundary.decode('ascii')


def encode_multipart_formdata(fields: List[Tuple[Dict[str, str], bytes]],
                              boundary: str) -> Tuple[bytes, str]:
    """
    Stolen from urllib3.filepost.encode_multipart_formdata() as of v1.26.2.

    Very heavily modified to be compatible with our gcloud-rest converter and
    to avboid unnecessary urllib3 dependencies (since that's only included with
    requests, not aiohttp).
    """
    body: List[bytes] = []
    for headers, data in fields:
        body.append(f'--{boundary}\r\n'.encode('utf-8'))

        # The below is from RequestFields.render_headers()
        # Since we only use Content-Type, we could simplify the below to a
        # single line... but probably best to be safe for future modifications.
        for field in ['Content-Disposition', 'Content-Type',
                      'Content-Location']:
            value = headers.pop(field, None)
            if value:
                body.append(f'{field}: {value}\r\n'.encode('utf-8'))
        for field, value in headers.items():
            # N.B. potential bug copied from urllib3 code; zero values should
            # be sent! Keeping it for now, since Google libs use urllib3 for
            # their examples.
            if value:
                body.append(f'{field}: {value}\r\n'.encode('utf-8'))

        body.append(b'\r\n')
        body.append(data)
        body.append(b'\r\n')

    body.append(f'--{boundary}--\r\n'.encode('utf-8'))

    # N.B. 'multipart/form-data' in upstream, but Google wants 'related'
    content_type = f'multipart/related; boundary={boundary}'

    return b''.join(body), content_type


class UploadType(enum.Enum):
    SIMPLE = 1
    RESUMABLE = 2
    MULTIPART = 3  # unused: SIMPLE upgrades to MULTIPART when metadata exists


class Storage:
    def __init__(self, *,
                 service_file: Optional[Union[str, IO[AnyStr]]] = None,
                 token: Optional[Token] = None,
                 session: Optional[Session] = None) -> None:
        self.session = AioSession(session, verify_ssl=VERIFY_SSL)
        self.token = token or Token(service_file=service_file, scopes=SCOPES,
                                    session=self.session.session)

    async def _headers(self) -> Dict[str, str]:
        if STORAGE_EMULATOR_HOST:
            return {}

        token = await self.token.get()
        return {
            'Authorization': f'Bearer {token}',
        }

    def get_bucket(self, bucket_name: str) -> Bucket:
        return Bucket(self, bucket_name)

    async def copy(self, bucket: str, object_name: str,
                   destination_bucket: str, *, new_name: Optional[str] = None,
                   params: Optional[Dict[str, str]] = None,
                   headers: Optional[Dict[str, str]] = None, timeout: int = 10,
                   session: Optional[Session] = None) -> Dict[str, Any]:

        """
        When files are too large, multiple calls to `rewriteTo` are made. We
        refer to the same copy job by using the `rewriteToken` from the
        previous return payload in subsequent `rewriteTo` calls.

        Using the `rewriteTo` GCS API is preferred in part because it is able
        to make multiple calls to fully copy an object whereas the `copyTo` GCS
        API only calls `rewriteTo` once under the hood, and thus may fail if
        files are large.

        In the rare case you need to resume a copy operation, include the
        `rewriteToken` in the `params` dictionary. Once you begin a multi-part
        copy operation, you then have 1 week to complete the copy job.

        https://cloud.google.com/storage/docs/json_api/v1/objects/rewrite
        """
        if not new_name:
            new_name = object_name

        url = (f"{API_ROOT}/{bucket}/o/{quote(object_name, safe='')}/rewriteTo"
               f"/b/{destination_bucket}/o/{quote(new_name, safe='')}")

        # We may optionally supply metadata* to apply to the rewritten
        # object, which explains why `rewriteTo` is a POST endpoint; however,
        # we don't expose that here so we have to send an empty body. Therefore
        # the `Content-Length` and `Content-Type` indicate an empty body.
        #
        # * https://cloud.google.com/storage/docs/json_api/v1/objects#resource
        headers = headers or {}
        headers.update(await self._headers())
        headers.update({
            'Content-Length': '0',
            'Content-Type': '',
        })

        params = params or {}

        s = AioSession(session) if session else self.session
        resp = await s.post(url, headers=headers, params=params,
                            timeout=timeout)

        data: Dict[str, Any] = await resp.json(content_type=None)

        while not data.get('done') and data.get('rewriteToken'):
            params['rewriteToken'] = data['rewriteToken']
            resp = await s.post(url, headers=headers, params=params,
                                timeout=timeout)
            data = await resp.json(content_type=None)

        return data

    async def delete(self, bucket: str, object_name: str, *, timeout: int = 10,
                     params: Optional[Dict[str, str]] = None,
                     headers: Optional[Dict[str, str]] = None,
                     session: Optional[Session] = None) -> str:
        # https://cloud.google.com/storage/docs/json_api/#encoding
        encoded_object_name = quote(object_name, safe='')
        url = f'{API_ROOT}/{bucket}/o/{encoded_object_name}'
        headers = headers or {}
        headers.update(await self._headers())

        s = AioSession(session) if session else self.session
        resp = await s.delete(url, headers=headers, params=params or {},
                              timeout=timeout)

        try:
            data: str = await resp.text()
        except (AttributeError, TypeError):
            data = str(resp.text)

        return data

    async def download(self, bucket: str, object_name: str, *,
                       headers: Optional[Dict[str, Any]] = None,
                       timeout: int = 10,
                       session: Optional[Session] = None) -> bytes:
        return await self._download(bucket, object_name, headers=headers,
                                    timeout=timeout, params={'alt': 'media'},
                                    session=session)

    async def download_to_filename(self, bucket: str, object_name: str,
                                   filename: str, **kwargs: Any) -> None:
        with open(filename, 'wb+') as file_object:
            content = await self.get_content(bucket, object_name,
                                                  **kwargs)
            while True:
                chunk = await content.read(WRITE_BUFFER_SIZE)
                if not chunk:
                    break
                file_object.write(chunk)

    async def download_metadata(self, bucket: str, object_name: str, *,
                                headers: Optional[Dict[str, Any]] = None,
                                session: Optional[Session] = None,
                                timeout: int = 10) -> Dict[str, Any]:
        data = await self._download(bucket, object_name, headers=headers,
                                    timeout=timeout, session=session)
        metadata: Dict[str, Any] = json.loads(data.decode())
        return metadata

    async def get_content(self, bucket: str, object_name: str, *,
                       headers: Optional[Dict[str, Any]] = None,
                       timeout: int = 10,
                       session: Optional[Session] = None) -> bytes:
        return await self._download(bucket, object_name, headers=headers,
                                    timeout=timeout, params={'alt': 'media'},
                                    session=session, get_content=True)

    async def list_objects(self, bucket: str, *,
                           params: Optional[Dict[str, str]] = None,
                           headers: Optional[Dict[str, Any]] = None,
                           session: Optional[Session] = None,
                           timeout: int = 10) -> Dict[str, Any]:
        url = f'{API_ROOT}/{bucket}/o'
        headers = headers or {}
        headers.update(await self._headers())

        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {},
                           timeout=timeout)
        data: Dict[str, Any] = await resp.json(content_type=None)
        return data

    # https://cloud.google.com/storage/docs/json_api/v1/how-tos/upload
    # pylint: disable=too-many-locals
    async def upload(self, bucket: str, object_name: str, file_data: Any,
                     *, content_type: Optional[str] = None,
                     parameters: Optional[Dict[str, str]] = None,
                     headers: Optional[Dict[str, str]] = None,
                     metadata: Optional[Dict[str, str]] = None,
                     session: Optional[Session] = None,
                     force_resumable_upload: Optional[bool] = None,
                     timeout: int = 30) -> Dict[str, Any]:
        url = f'{API_ROOT_UPLOAD}/{bucket}/o'

        stream = self._preprocess_data(file_data)

        if BUILD_GCLOUD_REST and isinstance(stream, io.StringIO):
            # HACK: `requests` library does not accept `str` as `data` in `put`
            # HTTP request.
            stream = io.BytesIO(stream.getvalue().encode('utf-8'))

        content_length = self._get_stream_len(stream)

        # mime detection method same as in aiohttp 3.4.4
        content_type = content_type or mimetypes.guess_type(object_name)[0]

        parameters = parameters or {}

        headers = headers or {}
        headers.update(await self._headers())
        headers.update({
            'Content-Length': str(content_length),
            'Content-Type': content_type or '',
        })

        upload_type = self._decide_upload_type(force_resumable_upload,
                                               content_length)
        log.debug('using %r gcloud storage upload method', upload_type)

        if upload_type == UploadType.RESUMABLE:
            return await self._upload_resumable(
                url, object_name, stream, parameters, headers,
                metadata=metadata, session=session, timeout=timeout)
        if upload_type == UploadType.SIMPLE:
            if metadata:
                return await self._upload_multipart(
                    url, object_name, stream, parameters, headers, metadata,
                    session=session, timeout=timeout)
            return await self._upload_simple(
                url, object_name, stream, parameters, headers, session=session,
                timeout=timeout)

        raise TypeError(f'upload type {upload_type} not supported')

    async def upload_from_filename(self, bucket: str, object_name: str,
                                   filename: str,
                                   **kwargs: Any) -> Dict[str, Any]:
        with open(filename, 'rb') as file_object:
            return await self.upload(bucket, object_name, file_object,
                                     **kwargs)

    @staticmethod
    def _get_stream_len(stream: IO[AnyStr]) -> int:
        current = stream.tell()
        try:
            return stream.seek(0, os.SEEK_END)
        finally:
            stream.seek(current)

    @staticmethod
    def _preprocess_data(data: Any) -> IO[Any]:
        if data is None:
            return io.StringIO('')

        if isinstance(data, bytes):
            return io.BytesIO(data)
        if isinstance(data, str):
            return io.StringIO(data)
        if isinstance(data, io.IOBase):
            return data  # type: ignore[return-value]

        raise TypeError(f'unsupported upload type: "{type(data)}"')

    @staticmethod
    def _decide_upload_type(force_resumable_upload: Optional[bool],
                            content_length: int) -> UploadType:
        # force resumable
        if force_resumable_upload is True:
            return UploadType.RESUMABLE

        # force simple
        if force_resumable_upload is False:
            return UploadType.SIMPLE

        # decide based on Content-Length
        if content_length > MAX_CONTENT_LENGTH_SIMPLE_UPLOAD:
            return UploadType.RESUMABLE

        return UploadType.SIMPLE

    @staticmethod
    def _split_content_type(content_type: str) -> Tuple[str, Optional[str]]:
        content_type_and_encoding_split = content_type.split(';')
        content_type = content_type_and_encoding_split[0].lower().strip()

        encoding = None
        if len(content_type_and_encoding_split) > 1:
            encoding_str = content_type_and_encoding_split[1].lower().strip()
            encoding = encoding_str.split('=')[-1]

        return content_type, encoding

    @staticmethod
    def _format_metadata_key(key: str) -> str:
        """
        Formats the fixed-key metadata keys as wanted by the multipart API.

        Ex: Content-Disposition --> contentDisposition
        """
        parts = key.split('-')
        parts = [parts[0].lower()] + [p.capitalize() for p in parts[1:]]
        return ''.join(parts)

    async def _download(self, bucket: str, object_name: str, *,
                        params: Optional[Dict[str, str]] = None,
                        headers: Optional[Dict[str, str]] = None,
                        timeout: int = 10,
                        session: Optional[Session] = None,
                        get_content: bool = False) -> bytes:
        # https://cloud.google.com/storage/docs/json_api/#encoding
        encoded_object_name = quote(object_name, safe='')
        url = f'{API_ROOT}/{bucket}/o/{encoded_object_name}'
        headers = headers or {}
        headers.update(await self._headers())

        s = AioSession(session) if session else self.session
        response = await s.get(url, headers=headers, params=params or {},
                               timeout=timeout)

        if get_content:
            return response.content

        # N.B. the GCS API sometimes returns 'application/octet-stream' when a
        # string was uploaded. To avoid potential weirdness, always return a
        # bytes object.
        try:
            data: bytes = await response.read()
        except (AttributeError, TypeError):
            data = response.content

        return data

    async def _upload_simple(self, url: str, object_name: str,
                             stream: IO[AnyStr], params: Dict[str, str],
                             headers: Dict[str, str], *,
                             session: Optional[Session] = None,
                             timeout: int = 30) -> Dict[str, Any]:
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/simple-upload
        params['name'] = object_name
        params['uploadType'] = 'media'

        s = AioSession(session) if session else self.session
        resp = await s.post(url, data=stream, headers=headers, params=params,
                            timeout=timeout)
        data: Dict[str, Any] = await resp.json(content_type=None)
        return data

    async def _upload_multipart(self, url: str, object_name: str,
                                stream: IO[AnyStr], params: Dict[str, str],
                                headers: Dict[str, str],
                                metadata: Dict[str, str], *,
                                session: Optional[Session] = None,
                                timeout: int = 30) -> Dict[str, Any]:
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/multipart-upload
        params['uploadType'] = 'multipart'

        metadata_headers = {'Content-Type': 'application/json; charset=UTF-8'}
        metadata = {self._format_metadata_key(k): v
                    for k, v in metadata.items()}
        metadata['name'] = object_name

        raw_body: AnyStr = stream.read()
        if isinstance(raw_body, str):
            bytes_body: bytes = raw_body.encode('utf-8')
        else:
            bytes_body = raw_body

        parts = [
            (metadata_headers, json.dumps(metadata).encode('utf-8')),
            ({'Content-Type': headers['Content-Type']}, bytes_body),
        ]
        boundary = choose_boundary()
        body, content_type = encode_multipart_formdata(parts, boundary)
        headers.update({
            'Content-Type': content_type,
            'Content-Length': str(len(body)),
            'Accept': 'application/json'
        })

        s = AioSession(session) if session else self.session
        resp = await s.post(url, data=body, headers=headers, params=params,
                            timeout=timeout)
        data: Dict[str, Any] = await resp.json(content_type=None)
        return data

    async def _upload_resumable(self, url: str, object_name: str,
                                stream: IO[AnyStr], params: Dict[str, str],
                                headers: Dict[str, str], *,
                                metadata: Optional[Dict[str, Any]] = None,
                                session: Optional[Session] = None,
                                timeout: int = 30) -> Dict[str, Any]:
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/resumable-upload
        session_uri = await self._initiate_upload(url, object_name, params,
                                                  headers, metadata=metadata,
                                                  session=session)
        return await self._do_upload(session_uri, stream, headers=headers,
                                     session=session, timeout=timeout)

    async def _initiate_upload(self, url: str, object_name: str,
                               params: Dict[str, str], headers: Dict[str, str],
                               *, metadata: Optional[Dict[str, str]] = None,
                               session: Optional[Session] = None) -> str:
        params['uploadType'] = 'resumable'

        metadict = (metadata or {}).copy()
        metadict.update({'name': object_name})
        metadata_ = json.dumps(metadict)

        post_headers = headers.copy()
        post_headers.update({
            'Content-Length': str(len(metadata_)),
            'Content-Type': 'application/json; charset=UTF-8',
            'X-Upload-Content-Type': headers['Content-Type'],
            'X-Upload-Content-Length': headers['Content-Length']
        })

        s = AioSession(session) if session else self.session
        resp = await s.post(url, headers=post_headers, params=params,
                            data=metadata_, timeout=10)
        session_uri: str = resp.headers['Location']
        return session_uri

    async def _do_upload(self, session_uri: str, stream: IO[AnyStr],
                         headers: Dict[str, str], *, retries: int = 5,
                         session: Optional[Session] = None,
                         timeout: int = 30) -> Dict[str, Any]:
        s = AioSession(session) if session else self.session

        for tries in range(retries):
            try:
                resp = await s.put(session_uri, headers=headers,
                                   data=stream, timeout=timeout)
            except ResponseError:
                headers.update({'Content-Range': '*/*'})
                await sleep(2. ** tries)  # type: ignore[func-returns-value]

                continue

            break

        data: Dict[str, Any] = await resp.json(content_type=None)
        return data

    async def get_bucket_metadata(self, bucket: str, *,
                                  params: Optional[Dict[str, str]] = None,
                                  headers: Optional[Dict[str, str]] = None,
                                  session: Optional[Session] = None,
                                  timeout: int = 10) -> Dict[str, Any]:
        url = f'{API_ROOT}/{bucket}'
        headers = headers or {}
        headers.update(await self._headers())

        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {},
                           timeout=timeout)
        data: Dict[str, Any] = await resp.json(content_type=None)
        return data

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> 'Storage':
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
