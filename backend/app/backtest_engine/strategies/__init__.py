"""内置策略：导入子模块即完成 register_strategy 注册。"""

from . import adx_macd  # noqa: F401
from . import atr_and_sizing  # noqa: F401
from . import boll  # noqa: F401
from . import cross_section  # noqa: F401
from . import donchian  # noqa: F401
from . import ema_sma_bias  # noqa: F401
from . import kdj  # noqa: F401
from . import ma_cross  # noqa: F401
from . import macd  # noqa: F401
from . import momentum_roc  # noqa: F401
from . import oscillator_extra  # noqa: F401
from . import rsi  # noqa: F401
from . import rsi_ma200  # noqa: F401
from . import triple_ma  # noqa: F401
from . import volume_price  # noqa: F401
