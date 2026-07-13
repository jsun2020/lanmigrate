"""Pairing tests: code format, password derivation, device memory (PRD F1)."""
import pytest

from lanmigrate import pairing


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pairing, "DEVICE_ID_FILE", tmp_path / "device_id")
    monkeypatch.setattr(pairing, "DEVICES_FILE", tmp_path / "devices.json")
    yield


def test_code_is_six_digits():
    for _ in range(50):
        code = pairing.generate_code()
        assert len(code) == 6 and code.isdigit()


def test_password_deterministic_and_code_bound():
    assert pairing.session_password("123456") == pairing.session_password("123456")
    assert pairing.session_password("123456") != pairing.session_password("123457")
    assert len(pairing.session_password("000000")) == 20


def test_fingerprint_stable_across_calls():
    fp1 = pairing.device_fingerprint()
    fp2 = pairing.device_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 12


def test_remember_and_recall_device():
    pairing.remember_device("abc123def456", "Jason-MacBook", "654321")
    dev = pairing.recall_device("abc123def456")
    assert dev == {"name": "Jason-MacBook", "code": "654321"}
    assert pairing.recall_device("unknown") is None
