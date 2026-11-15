
def settle_up(balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """
    Calculate transfers to settle group balances.

    Positive balance: should receive money.
    Negative balance: owes money.

    Returns (from_user, to_user, amount_cents).
    """
    if any(type(v) is not int for v in balances.values()):
        raise ValueError("Balances must be integer cents")

    if sum(balances.values()) != 0:
        raise ValueError("Balances must sum to zero")

    debtors = [
        [uid, -amount]
        for uid, amount in sorted(balances.items())
        if amount < 0
    ]

    creditors = [
        [uid, amount]
        for uid, amount in sorted(balances.items())
        if amount > 0
    ]

    transfers = []
    i, j = 0, 0

    while i < len(debtors) and j < len(creditors):
        debtor_id, debt = debtors[i]
        creditor_id, credit = creditors[j]

        payment = min(debt, credit)
        transfers.append(
            (debtor_id, creditor_id, payment)
        )

        debtors[i][1] -= payment
        creditors[j][1] -= payment

        if debtors[i][1] == 0:
            i += 1

        if creditors[j][1] == 0:
            j += 1

    return transfers
