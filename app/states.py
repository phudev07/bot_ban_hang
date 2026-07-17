from aiogram.fsm.state import State, StatesGroup


class DepositStates(StatesGroup):
    waiting_for_amount = State()


class PurchaseStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_coupon = State()
    waiting_for_coupon_quantity = State()


class BroadcastStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_confirmation = State()
