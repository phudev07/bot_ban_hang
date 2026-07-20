from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit, FlashSaleCampaign, Product


class FlashSaleUnavailable(RuntimeError):
    pass


def unsafe_flash_sale_status(
    campaign: FlashSaleCampaign,
    product: Product,
) -> str | None:
    if product.supplier_price is not None and campaign.sale_price < product.supplier_price:
        return "cost_exceeded"
    if campaign.sale_price >= product.price:
        return "price_invalid"
    return None


def stop_unsafe_flash_sale(
    campaign: FlashSaleCampaign,
    product: Product,
) -> str | None:
    status = unsafe_flash_sale_status(campaign, product)
    if status is None:
        return None
    campaign.status = status
    campaign.ended_at = datetime.now(UTC)
    if campaign.notification_status in {"pending", "sending"}:
        campaign.notification_status = "superseded"
    return status


def flash_sale_remaining(campaign: FlashSaleCampaign) -> int:
    return max(
        0,
        campaign.total_quantity
        - campaign.sold_quantity
        - campaign.reserved_quantity,
    )


async def active_flash_sale(
    session: AsyncSession,
    product_id: int,
    *,
    quantity: int = 1,
    for_update: bool = False,
    campaign_id: int | None = None,
) -> FlashSaleCampaign | None:
    statement = (
        select(FlashSaleCampaign)
        .join(Product, Product.id == FlashSaleCampaign.product_id)
        .where(
            FlashSaleCampaign.product_id == product_id,
            FlashSaleCampaign.status == "active",
            FlashSaleCampaign.sale_price < Product.price,
            or_(
                Product.supplier_price.is_(None),
                FlashSaleCampaign.sale_price >= Product.supplier_price,
            ),
        )
        .order_by(FlashSaleCampaign.id.desc())
        .limit(1)
    )
    if campaign_id is not None:
        statement = statement.where(FlashSaleCampaign.id == campaign_id)
    if for_update:
        statement = statement.with_for_update()
    campaign = await session.scalar(statement)
    if campaign is None or flash_sale_remaining(campaign) < max(1, quantity):
        return None
    return campaign


async def active_flash_sale_prices(
    session: AsyncSession,
    product_ids: list[int] | tuple[int, ...],
) -> dict[int, int]:
    return {
        product_id: campaign.sale_price
        for product_id, campaign in (
            await active_flash_sale_campaigns(session, product_ids)
        ).items()
    }


async def active_flash_sale_campaigns(
    session: AsyncSession,
    product_ids: list[int] | tuple[int, ...],
) -> dict[int, FlashSaleCampaign]:
    if not product_ids:
        return {}
    campaigns = list(
        await session.scalars(
            select(FlashSaleCampaign)
            .join(Product, Product.id == FlashSaleCampaign.product_id)
            .where(
                FlashSaleCampaign.product_id.in_(product_ids),
                FlashSaleCampaign.status == "active",
                FlashSaleCampaign.sale_price < Product.price,
                or_(
                    Product.supplier_price.is_(None),
                    FlashSaleCampaign.sale_price >= Product.supplier_price,
                ),
            )
            .order_by(FlashSaleCampaign.id.desc())
        )
    )
    result: dict[int, FlashSaleCampaign] = {}
    for campaign in campaigns:
        if campaign.product_id in result or flash_sale_remaining(campaign) <= 0:
            continue
        result[campaign.product_id] = campaign
    return result


def consume_flash_sale(campaign: FlashSaleCampaign | None, quantity: int) -> None:
    if campaign is None or quantity < 1:
        return
    if flash_sale_remaining(campaign) < quantity:
        raise FlashSaleUnavailable("Flash sale quantity is no longer available")
    campaign.sold_quantity += quantity
    if campaign.sold_quantity + campaign.reserved_quantity >= campaign.total_quantity:
        campaign.status = "completed"
        campaign.ended_at = datetime.now(UTC)


def reserve_flash_sale(campaign: FlashSaleCampaign, quantity: int) -> None:
    if quantity < 1 or flash_sale_remaining(campaign) < quantity:
        raise FlashSaleUnavailable("Flash sale quantity is no longer available")
    campaign.reserved_quantity += quantity
    if campaign.sold_quantity + campaign.reserved_quantity >= campaign.total_quantity:
        campaign.status = "completed"
        campaign.ended_at = datetime.now(UTC)


def complete_flash_sale_reservation(
    campaign: FlashSaleCampaign,
    quantity: int,
) -> None:
    reserved = min(max(0, quantity), campaign.reserved_quantity)
    campaign.reserved_quantity -= reserved
    campaign.sold_quantity += reserved
    if campaign.sold_quantity + campaign.reserved_quantity >= campaign.total_quantity:
        campaign.status = "completed"
        campaign.ended_at = datetime.now(UTC)


def release_flash_sale_reservation(
    campaign: FlashSaleCampaign | None,
    quantity: int,
) -> None:
    if campaign is None or quantity < 1:
        return
    campaign.reserved_quantity = max(0, campaign.reserved_quantity - quantity)
    if campaign.status == "completed" and campaign.sold_quantity < campaign.total_quantity:
        campaign.status = "active"
        campaign.ended_at = None


async def complete_deposit_flash_sale(
    session: AsyncSession,
    deposit: Deposit,
) -> FlashSaleCampaign | None:
    if deposit.flash_sale_id is None or deposit.flash_sale_quantity < 1:
        return None
    campaign = await session.scalar(
        select(FlashSaleCampaign)
        .where(FlashSaleCampaign.id == deposit.flash_sale_id)
        .with_for_update()
    )
    if campaign is not None:
        complete_flash_sale_reservation(campaign, deposit.flash_sale_quantity)
    deposit.flash_sale_quantity = 0
    return campaign


async def release_deposit_flash_sale(
    session: AsyncSession,
    deposit: Deposit,
) -> None:
    if deposit.flash_sale_id is None or deposit.flash_sale_quantity < 1:
        return
    campaign = await session.scalar(
        select(FlashSaleCampaign)
        .where(FlashSaleCampaign.id == deposit.flash_sale_id)
        .with_for_update()
    )
    release_flash_sale_reservation(campaign, deposit.flash_sale_quantity)
    deposit.flash_sale_quantity = 0
