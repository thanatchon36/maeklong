"""Configuration (SPEC §6), validated by pydantic. Secrets never live here — env only."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverbank.core.models import Mode

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISK"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountCfg(_Strict):
    fee_maker_bps: float = Field(2.0, ge=-5, le=50)
    fee_taker_bps: float = Field(5.0, ge=0, le=50)
    # SPEC sample: 3. With isolated margin, 3x puts a short's liquidation only ~30.1 % away, so the
    # 30 % min-liquidation-distance rule would bind immediately (OPEN_QUESTIONS B19): 2x is used.
    leverage: int = Field(2, ge=1, le=125)
    margin_type: Literal["ISOLATED"] = "ISOLATED"


class UniverseCfg(_Strict):
    max_active_symbols: int = Field(3, ge=0)
    max_candidate_symbols: int = Field(20, ge=0)
    min_listing_days: int = Field(30, ge=0)
    edge_ratio_min: float = Field(1.5, gt=0)
    vol_min_musd: float = Field(5, ge=0)
    vol_max_musd: float = Field(300, gt=0)
    toxicity_max: float = Field(400, gt=0)
    min_fills_for_promotion: int = Field(300, ge=1)
    demotion_window_fills: int = Field(500, ge=1)
    screen_interval_s: int = Field(300, ge=10)
    screen_lookback_days: int = Field(7, ge=1)
    hedge_only: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    allowed_underlying_types: tuple[str, ...] = ("COIN",)
    blacklist_path: str = "config/blacklist.yaml"
    # Explicit symbol list (e.g. demo); hard exclusions still apply. Empty = use screener.
    static_symbols: tuple[str, ...] = ()
    # Live allowlist chosen by the operator (M5). Only these can be ACTIVE unless auto_promote.
    live_symbols: tuple[str, ...] = ()
    auto_promote: bool = False
    promotion_horizon_s: int = 60
    demotion_cooldown_h: float = Field(24, ge=0)
    # Live (M5): shadow evidence for promotion comes from the shadow service's DB, opened read-only.
    shadow_db: str = "state/riverbank.sqlite"


class QuotingCfg(_Strict):
    order_size_usdt: float = Field(10, gt=0)
    size_buffer: float = Field(2.0, ge=1.0)
    max_inventory_units: int = Field(5, ge=1)
    min_half_spread_bps: float = Field(3.0, gt=0)
    base_half_spread_bps: float = Field(2.0, ge=0)
    vol_mult: float = Field(1.0, ge=0)
    skew_k: float = Field(0.5, ge=0)
    requote_ticks: int = Field(1, ge=1)
    min_requote_interval_ms: int = Field(1000, ge=0)  # only for moves toward the touch (OPEN_QUESTIONS B18)
    vol_kill_bps: float = Field(50, gt=0)
    cooldown_s: int = Field(60, ge=0)
    toxic_markout_bps: float = -3.0
    funding_guard_s: int = Field(60, ge=0)
    # SPEC sample shows `widen`; OPEN_QUESTIONS.md picks the conservative `pull`.
    funding_guard_action: Literal["widen", "pull"] = "pull"
    funding_widen_mult: float = Field(2.0, ge=1.0)
    sigma_horizon_s: float = Field(5.0, gt=0)
    sigma_half_life_s: float = Field(60.0, gt=0)
    sigma_warmup_samples: int = Field(30, ge=2)
    flow_half_life_s: float = Field(5.0, gt=0)
    imbalance_levels: int = Field(5, ge=1)
    alpha_imb: float = 0.0
    alpha_flow: float = 0.0
    quote_interval_ms: int = Field(100, ge=10)
    toxicity_window_fills: int = Field(20, ge=1)
    toxicity_min_samples: int = Field(5, ge=1)
    toxicity_step_s: int = Field(30, ge=1)
    toxicity_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    levels: int = Field(1, ge=1, le=5)  # v2 multi-level; 1 = v1 behaviour
    level_spacing_bps: float = Field(5.0, gt=0)


class RiskCfg(_Strict):
    max_position_usdt: float = Field(50, gt=0)
    max_gross_exposure_usdt: float = Field(150, gt=0)
    max_net_beta_exposure_usdt: float = Field(75, gt=0)
    max_open_orders: int = Field(20, ge=1)
    max_order_rate_frac: float = Field(0.5, gt=0, le=1.0)
    daily_loss_limit_pct: float = Field(10, gt=0, le=100)
    max_drawdown_pct: float = Field(15, gt=0, le=100)
    max_leverage: float = Field(3, gt=0)
    min_liq_distance_pct: float = Field(30, ge=0, le=100)
    emergency_flatten: bool = False
    daily_loss_alert_frac: float = Field(0.5, gt=0, le=1)
    resume_frac: float = Field(0.9, gt=0, le=1)  # REDUCE_ONLY -> OK hysteresis
    flatten_requote_s: float = Field(3.0, gt=0)
    flatten_taker_after_s: float = Field(30.0, gt=0)


class HedgeCfg(_Strict):
    enabled: bool = True
    instruments: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    hedge_threshold_usdt: float = Field(25, gt=0)
    hedge_urgent_usdt: float = Field(60, gt=0)
    allow_taker: bool = False  # OPEN_QUESTIONS.md: post-only only
    beta_window_days: float = Field(3, gt=0)
    beta_shrink: float = Field(0.3, ge=0, le=1)
    beta_refresh_s: int = Field(3600, ge=60)
    eval_interval_s: float = Field(5.0, gt=0)


class TelegramCfg(_Strict):
    enabled: bool = True  # still requires TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env
    poll_commands: bool = True


class OpsCfg(_Strict):
    stale_ms: int = Field(3000, ge=100)
    reconcile_s: int = Field(30, ge=1)
    deadman_heartbeat_s: int = Field(10, ge=1)
    deadman_countdown_s: int = Field(30, ge=5)
    sim_latency_ms: int = Field(150, ge=0)
    recon_grace_ms: int = Field(3000, ge=0)
    metrics_port: int = 9108
    log_level: str = "INFO"
    log_levels: Mapping[str, str] = Field(default_factory=dict)
    data_dir: str = "data"
    state_dir: str = "state"
    reports_dir: str = "reports"
    kill_file: str = "KILL"
    raw_retention_days: int = Field(30, ge=1)
    record_top_n: int = Field(20, ge=1, le=100)
    record_depth_diffs: bool = False
    recorder_flush_s: int = Field(60, ge=1)
    symbols_per_connection: int = Field(10, ge=1, le=200)
    ws_roll_hours: float = Field(23.0, gt=0, lt=24)
    depth_snapshot_limit: Literal[100, 500, 1000] = 1000
    resync_min_interval_s: float = Field(5.0, ge=0)
    exchange_info_refresh_s: int = Field(3600, ge=60)
    shadow_equity_usdt: float = Field(300, gt=0)
    recv_window_ms: int = Field(5000, ge=1000, le=60000)
    account_refresh_s: int = Field(30, ge=5)
    telegram: TelegramCfg = TelegramCfg()


class Config(_Strict):
    mode: Mode = Mode.SHADOW
    account: AccountCfg = AccountCfg()
    universe: UniverseCfg = UniverseCfg()
    quoting: QuotingCfg = QuotingCfg()
    risk: RiskCfg = RiskCfg()
    hedge: HedgeCfg = HedgeCfg()
    ops: OpsCfg = OpsCfg()

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        if self.universe.vol_min_musd > self.universe.vol_max_musd:
            raise ValueError("universe.vol_min_musd > vol_max_musd")
        if self.quoting.min_half_spread_bps <= self.account.fee_maker_bps:
            raise ValueError("quoting.min_half_spread_bps must exceed account.fee_maker_bps")
        if self.account.leverage > self.risk.max_leverage:
            raise ValueError("account.leverage exceeds risk.max_leverage")
        if self.ops.deadman_countdown_s <= 2 * self.ops.deadman_heartbeat_s:
            raise ValueError("deadman_countdown_s must be > 2x deadman_heartbeat_s")
        if self.hedge.hedge_urgent_usdt < self.hedge.hedge_threshold_usdt:
            raise ValueError("hedge_urgent_usdt < hedge_threshold_usdt")
        return self


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config.model_validate(raw)


class SafetyError(RuntimeError):
    """Raised when a safety gate refuses an action."""


def resolve_mode(cfg_mode: Mode, env: Mapping[str, str] | None = None) -> Mode:
    """Effective mode. Env MODE overrides config. LIVE needs MODE=live AND LIVE_CONFIRM in env.

    Config alone can never enable live (SPEC §0.2, §11.1).
    """
    env = os.environ if env is None else env
    env_mode = env.get("MODE", "").strip().lower()
    mode = Mode(env_mode) if env_mode else cfg_mode
    if mode is Mode.LIVE:
        if env_mode != "live":
            raise SafetyError("live mode requires MODE=live in the environment (config alone is not enough)")
        if env.get("LIVE_CONFIRM", "") != LIVE_CONFIRM_PHRASE:
            raise SafetyError(f"live mode requires LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}")
    return mode
