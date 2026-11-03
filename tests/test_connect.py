import requests

from ktrader.config import KISConfig
from ktrader.connect import check_env, connection_check


class _R:
    def __init__(self, js, status=200):
        self._js, self.status_code = js, status

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden")


class _OkSession:
    def __init__(self):
        self.methods = []

    def post(self, url, json=None, timeout=None):
        return _R({"access_token": "tok", "expires_in": 86400})

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.methods.append(method)
        if "domestic-stock/v1/quotations/inquire-price" in url:
            return _R({"rt_cd": "0", "output": {"stck_prpr": "70000", "stck_sdpr": "69500", "stck_mxpr": "90300",
                                                 "stck_llam": "48650", "acml_vol": "1000"}})
        if "domestic-stock/v1/trading/inquire-balance" in url:
            return _R({"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "10000000", "tot_evlu_amt": "10000000"}]})
        if "overseas-price" in url:
            return _R({"rt_cd": "0", "output": {"last": "189.43", "base": "187.10", "tvol": "100"}})
        if "overseas-stock/v1/trading/inquire-balance" in url:
            return _R({"rt_cd": "0", "output1": [], "output2": {"frcr_dncl_amt_2": "0"}})
        return _R({"rt_cd": "1", "msg_cd": "X", "msg1": "unexpected"})


CFG = KISConfig("PSabcdefgh", "secret", "12345678-01", is_mock=True)


def test_all_steps_pass_and_read_only():
    s = _OkSession()
    res = connection_check(CFG, session=s)
    assert [r.step for r in res] == ["환경변수", "접근토큰 발급", "국내 현재가", "국내 잔고", "해외 현재가", "해외 잔고"]
    assert all(r.ok for r in res), [r.line() for r in res]
    assert set(s.methods) == {"GET"}               # 주문(POST) 없음
    assert "PSabcdefgh" not in res[0].detail        # 앱키 마스킹


def test_bad_key_stops_after_token_with_hint():
    class Bad(_OkSession):
        def post(self, url, json=None, timeout=None):
            return _R({}, 403)

    res = connection_check(CFG, session=Bad())
    assert len(res) == 2 and not res[1].ok and "모의/실전" in res[1].hint


def test_network_error_hint():
    class Down(_OkSession):
        def post(self, url, json=None, timeout=None):
            raise requests.ConnectionError("connection refused")

    res = connection_check(CFG, session=Down())
    assert not res[1].ok and "29443" in res[1].hint


def test_env_validation():
    assert not check_env(KISConfig("", "", "")).ok
    assert not check_env(KISConfig("k", "s", "1234-5")).ok


def test_load_targets_validation(tmp_path):
    import pytest

    from ktrader.cli import load_targets

    ok = tmp_path / "t.csv"
    ok.write_text("ticker,weight,reason\n005930,0.1,a\nAAPL,0.1,b\n7203.T,0.05,c\n", encoding="utf-8")
    assert load_targets(str(ok))["weight"].sum() == pytest.approx(0.25)
    for body in ("ticker,weight\n005930,10\n",            # 10 = 1000% 오입력
                 "ticker,weight\n005930,0.6\n000660,0.6\n",  # 합계 120%
                 "ticker,weight\n12345,0.1\n",              # 잘못된 코드
                 "ticker,weight\n005930,0.1\n005930,0.1\n"):  # 중복
        bad = tmp_path / "b.csv"
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError):
            load_targets(str(bad))
