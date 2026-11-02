from pricing import discount


def test_discount_reduces_price():
    assert discount(10.0, 3.0) == 7.0


def test_discount_past_zero():
    assert discount(10.0, 15.0) == 0.0
