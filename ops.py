    def __init__(self, runner: Any): # 'runner' is BotRunner instance

    def _get_thread_exchange(self, market_type: str) -> ExchangeInterface:

    def _generate_deterministic_id(self, bot_id: int, type_str: str, step_index: int) -> str:

    def _get_strategy_instance(self, bot_id: int, config_dict: Dict[str, Any], config_json_str: Optional[str] = None) -> MartingaleStrategy:

    def _get_phys_pos(self, pair: str, direction: str = None) -> Optional[Dict[str, Any]]:

    def _is_order_net_reducing(self, pair, side, qty):

    def _prepare_tp_order_params(self, bot_id, name, pair, side, amount, tp_price, current_price, exchange):

    def _place_gtx_order_with_retry(self, exchange, pair: str, side: str, amount: float, price: float, params: dict, label: str = "order") -> dict:

    def _get_order_amount(order: dict) -> float:

    def _compute_effective_tp(self, bot_id: int, name: str, bot_status: dict,

    def _sync_replace_tp(self, bot_id: int, name: str, pair: str, direction: str,

    def process_bot(self, bot_data: Tuple, exchange_snapshot: Dict[str, Any]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params=None, exchange=None, market_snapshot=None, bot_config=None, bot_status=None) -> Optional[Dict[str, Any]]:

    def execute_exit_tp(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):

    def _manage_hedge_exit(self, bot_id: int, name: str, pair: str, direction: str, 

    def maintain_orders(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:

    def execute_hedge_lock(self, bot_id: int, name: str, pair: str, direction: str,

    def execute_exit_sl(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):

    def _manage_hedge_exit(self, bot_id: int, name: str, pair: str, direction: str, 

    def check_for_safety_stop(self):
