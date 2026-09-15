"""后端会话删除必须存在可观察闭环。"""
from uuid import uuid4


def test_delete_missing_conversation_is_idempotent(client):
    response = client.delete('/v1/agent/conversations/' + str(uuid4()), headers={'X-Request-ID': str(uuid4())})
    assert response.status_code == 200
    assert response.json()['deleted'] is True
