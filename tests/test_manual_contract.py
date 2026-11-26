from uuid import uuid4

from agent.main import create_app
from common.protocol import Health


def test_manual_contract_is_optional_for_old_agents():
    assert not Health(protocol=1, robot="mock").manual_available


def test_manual_routes_are_registered():
    app = create_app(uuid4().hex)
    assert {"/manual", "/manual/{run_id}/stop"} <= {route.path for route in app.routes}
