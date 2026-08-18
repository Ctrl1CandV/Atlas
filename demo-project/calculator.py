"""一个故意有 bug 的计算器:is_prime 把 2 判成非素数,fizzbuzz 的 15 走错分支。"""


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    for i in range(2, n):
        if n % i == 0:
            return False
    return True


def fizzbuzz(n: int) -> str:
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)
