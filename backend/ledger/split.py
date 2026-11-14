
def split_equally(total_cents: int, user_ids: list[int]) -> dict[int, int]:
    """
    Split an expense equally among users.

    Money is stored in integer cents.
    Remaining cents are assigned by ascending user ID.
    """
    if type(total_cents) is not int or total_cents < 0:
        raise ValueError("Invalid total amount")

    if not user_ids or len(set(user_ids)) != len(user_ids):
        raise ValueError("Invalid users")

    if any(type(uid) is not int for uid in user_ids):
        raise ValueError("User IDs must be integers")

    sorted_ids = sorted(user_ids)
    base, remainder = divmod(total_cents, len(sorted_ids))

    return {
        uid: base + (1 if i < remainder else 0)
        for i, uid in enumerate(sorted_ids)
    }



def split_exact(
    total_cents: int,
    amounts: dict[int, int]
) -> dict[int, int]:
    """Split by exact amounts in integer cents."""

    if type(total_cents) is not int or total_cents < 0:
        raise ValueError("Invalid total amount")

    if not amounts:
        raise ValueError("No participants")

    if any(
        type(uid) is not int
        or type(amount) is not int
        or amount < 0
        for uid, amount in amounts.items()
    ):
        raise ValueError("Invalid user IDs or amounts")

    if sum(amounts.values()) != total_cents:
        raise ValueError("Shares do not match total")

    return dict(amounts)


def split_by_shares(
    total_cents: int,
    weights: dict[int, int]
) -> dict[int, int]:
    """Split proportionally by integer shares."""

    if type(total_cents) is not int or total_cents < 0:
        raise ValueError("Invalid total amount")

    if not weights or any(
        type(uid) is not int
        or type(weight) is not int
        or weight < 0
        for uid, weight in weights.items()
    ):
        raise ValueError("Invalid weights")

    total_weight = sum(weights.values())

    if total_weight <= 0:
        raise ValueError("Total weight must be positive")

    result = {}
    remainders = []

    for uid, weight in weights.items():
        numerator = total_cents * weight
        base, remainder = divmod(numerator, total_weight)

        result[uid] = base
        remainders.append((remainder, uid))

    remaining = total_cents - sum(result.values())

    # Largest remainder first; user ID breaks ties.
    remainders.sort(key=lambda item: (-item[0], item[1]))

    for _, uid in remainders[:remaining]:
        result[uid] += 1

    return result
