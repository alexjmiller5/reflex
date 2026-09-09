import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def provision():
    path = Path(__file__).parents[1] / "scripts/provision.py"
    spec = importlib.util.spec_from_file_location("provision_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_returns_one_verified_pair_without_config_or_browser_writes(
    provision, monkeypatch, capsys
):
    from modal.client import Client
    from modal.token_flow import TokenFlow
    import modal.config
    import webbrowser

    client = object()
    anonymous = Mock(return_value=nullcontext(client))
    monkeypatch.setattr(Client, "anonymous", anonymous)
    verify = Mock()
    monkeypatch.setattr(Client, "verify", verify)
    monkeypatch.setattr(
        TokenFlow,
        "start",
        lambda *_: nullcontext(("flow", "https://example.test/authorize", "code")),
    )
    finish = Mock(
        side_effect=[None, SimpleNamespace(token_id="fixture-id", token_secret="fixture-secret")]
    )
    monkeypatch.setattr(TokenFlow, "__init__", lambda *_: None)
    monkeypatch.setattr(TokenFlow, "finish", finish)
    monkeypatch.setattr(
        modal.config, "_store_user_config", lambda *_args, **_kw: pytest.fail("wrote config")
    )
    monkeypatch.setattr(webbrowser, "open", lambda *_: pytest.fail("opened local browser"))
    monkeypatch.setattr(
        Path, "write_text", lambda *_args, **_kw: pytest.fail("wrote credential cache")
    )
    monkeypatch.setattr(
        Path, "touch", lambda *_args, **_kw: pytest.fail("created credential cache")
    )
    monkeypatch.setenv("MODAL_TOKEN_ID", "unrelated-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "unrelated-secret")
    monkeypatch.setattr(provision.sys, "argv", ["provision.py", "--batch", "modal-token"])
    provision.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == {"token-id": "fixture-id", "token-secret": "fixture-secret"}
    assert "https://example.test/authorize" in output.err
    assert "fixture-secret" not in output.err
    anonymous.assert_called_once()
    assert verify.call_args.args[1] == ("fixture-id", "fixture-secret")
    assert finish.call_count == 2


@pytest.mark.parametrize("result", [None, SimpleNamespace(token_id="only-id", token_secret="")])
def test_failed_or_partial_flow_emits_no_credentials(provision, monkeypatch, capsys, result):
    from modal.client import Client
    from modal.token_flow import TokenFlow

    monkeypatch.setattr(Client, "anonymous", lambda *_: nullcontext(object()))
    monkeypatch.setattr(TokenFlow, "__init__", lambda *_: None)
    monkeypatch.setattr(
        TokenFlow, "start", lambda *_: nullcontext(("flow", "https://example.test", "code"))
    )
    monkeypatch.setattr(TokenFlow, "finish", lambda **_: result)
    monkeypatch.setattr(provision, "MAX_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(provision.sys, "argv", ["provision.py", "--batch", "modal-token"])
    with pytest.raises((RuntimeError, SystemExit)):
        provision.main()
    assert capsys.readouterr().out == ""


def test_pair_declared_and_individual_fields_refused(provision, monkeypatch, capsys):
    monkeypatch.setattr(provision.sys, "argv", ["provision.py", "--batches"])
    provision.main()
    assert json.loads(capsys.readouterr().out) == {"modal-token": ["token-id", "token-secret"]}
    monkeypatch.setattr(provision.sys, "argv", ["provision.py", "--field", "token-id"])
    with pytest.raises(SystemExit):
        provision.main()
    assert capsys.readouterr().out == ""
