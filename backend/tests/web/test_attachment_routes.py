"""图片上传、原图读取和草稿删除的真实 HTTP 闭环。"""
from uuid import uuid4
import pytest
from ..agent.test_image_validation import picture


def test_upload_read_and_delete(client):
    draft = str(uuid4())
    raw = picture("PNG")
    response = client.post('/v1/agent/attachments', headers={'X-Request-ID': str(uuid4())},
        data={'draft_id': draft}, files={'file': ('image.png', raw, 'image/png')})
    assert response.status_code == 201, response.text
    attachment = response.json()['attachment']
    url = '/v1/agent/attachments/' + attachment['attachment_id']
    response = client.get(url + '/content', headers={'X-Request-ID': str(uuid4())})
    assert response.content == raw
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    response = client.delete(url, params={'draft_id': draft}, headers={'X-Request-ID': str(uuid4())})
    assert response.status_code == 200
    assert response.headers['x-request-id'] == response.json()['request_id']
    assert client.get(url + '/content', headers={'X-Request-ID': str(uuid4())}).status_code == 404
    response = client.delete('/v1/agent/attachment-drafts/' + draft, headers={'X-Request-ID': str(uuid4())})
    assert response.headers['x-request-id'] == response.json()['request_id']


def test_rejects_corrupt_and_extra_parts(client):
    response = client.post('/v1/agent/attachments', headers={'X-Request-ID': str(uuid4())},
        data={'draft_id': str(uuid4())}, files={'file': ('image.png', b'bad', 'image/png')})
    assert response.status_code == 422


@pytest.mark.parametrize('origin', ['http://tauri.localhost', 'http://localhost:1420'])
def test_image_response_exposes_validation_headers_to_browser(client, origin):
    """跨域读取原图时，浏览器必须能读取前端严格校验所需的非默认响应头。"""
    raw = picture('PNG')
    response = client.post('/v1/agent/attachments', headers={'X-Request-ID': str(uuid4())},
        data={'draft_id': str(uuid4())}, files={'file': ('image.png', raw, 'image/png')})
    assert response.status_code == 201, response.text
    attachment_id = response.json()['attachment']['attachment_id']
    request_id = str(uuid4())
    response = client.get(f'/v1/agent/attachments/{attachment_id}/content',
        headers={'Origin': origin, 'X-Request-ID': request_id})
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers['access-control-allow-origin'] == origin
    exposed = {name.strip().lower() for name in
        response.headers['access-control-expose-headers'].split(',')}
    assert {'x-request-id', 'x-content-type-options'} <= exposed
    assert response.headers['x-request-id'] == request_id
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'] == 'image/png'


@pytest.mark.parametrize('path', ['/v1/agent/attachments', '/v1/agent/attachments/', '/v1/agent/turns'])
def test_actual_stream_size_enforced_without_content_length(client, path):
    """逐块流也受实际接收长度限制，路径变体不能继承图片上限。"""
    limit = 10_485_760 + 65_536 if path == '/v1/agent/attachments' else 1_048_576
    response = client.post(path, headers={'X-Request-ID': str(uuid4()), 'Content-Type': 'multipart/form-data; boundary=x'},
        content=(b'x' * min(65536, limit + 1 - offset) for offset in range(0, limit + 1, 65536)))
    assert response.status_code == 413
    assert response.json()['error_code'] == 'REQUEST_TOO_LARGE'
    response = client.post('/v1/agent/attachments', headers={'X-Request-ID': str(uuid4())},
        data={'draft_id': str(uuid4()), 'extra': 'bad'}, files={'file': ('image.png', picture('PNG'), 'image/png')})
    assert response.status_code == 422
