import argparse
import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.database import create_database
from app.models import Product
from app.supplier_audit import recover_supplier_order
from app.suppliers import create_sumistore_client, refresh_external_product
from app.utils import SecretCipher


def parse_audit_mapping(value: str) -> tuple[int, str]:
    audit_id, separator, order_code = value.partition(":")
    if not separator or not audit_id.isdigit() or not order_code.strip():
        raise argparse.ArgumentTypeError("Expected AUDIT_ID:SUMI_ORDER_CODE")
    return int(audit_id), order_code.strip()


async def recover(product_id: int, mappings: list[tuple[int, str]]) -> None:
    settings = get_settings()
    client = create_sumistore_client(settings)
    if client is None:
        raise RuntimeError("Sumistore is not configured")
    cipher = SecretCipher(settings.inventory_encryption_key.get_secret_value())
    engine, sessions = create_database(settings.database_url)
    try:
        async with sessions() as session:
            async with session.begin():
                recovered = [
                    await recover_supplier_order(
                        session,
                        client,
                        cipher,
                        audit_transaction_id=audit_id,
                        product_id=product_id,
                        supplier_order_code=order_code,
                    )
                    for audit_id, order_code in mappings
                ]
                product = await session.scalar(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
                if product is None:
                    raise RuntimeError("Product does not exist")
                await refresh_external_product(session, product, client)
        for item in recovered:
            print(
                f"Recovered {item.order_code}: accounts={item.account_count} "
                f"inserted={item.inserted_count} cost={item.total_cost}"
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover completed Sumi orders into stock")
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument(
        "--audit",
        type=parse_audit_mapping,
        action="append",
        required=True,
        help="AUDIT_ID:SUMI_ORDER_CODE; repeat for every orphan order",
    )
    args = parser.parse_args()
    asyncio.run(recover(args.product_id, args.audit))


if __name__ == "__main__":
    main()
