from calculator import fizzbuzz, is_prime


def test_two_is_prime():
    assert is_prime(2) is True


def test_fizzbuzz_15():
    assert fizzbuzz(15) == "FizzBuzz"


def test_fizzbuzz_9():
    assert fizzbuzz(9) == "Fizz"


def test_fizzbuzz_7():
    assert fizzbuzz(7) == "7"
