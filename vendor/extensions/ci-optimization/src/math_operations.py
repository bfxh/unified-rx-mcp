# math_operations.py

def add(a, b):
    """Return the sum of two numbers."""
    return a + b

def subtract(a, b):
    """Return the difference between two numbers."""
    return a - b

def multiply(a, b):
    """Return the product of two numbers."""
    return a * b

def divide(a, b):
    """Return the division of two numbers. Raise an error if dividing by zero."""
    if b == 0:
        raise ValueError("Cannot divide by zero.")
    return a / b

def power(base, exponent):
    """Return the base raised to the power of exponent."""
    return base ** exponent

def modulus(a, b):
    """Return the remainder of dividing a by b."""
    return a % b

def floor_divide(a, b):
    """Return the floor division of two numbers."""
    if b == 0:
        raise ValueError("Cannot divide by zero.")
    return a // b

def square_root(x):
    """Return the square root of a number. Raise an error if the number is negative."""
    if x < 0:
        raise ValueError("Cannot take the square root of a negative number.")
    return x ** 0.5

def absolute_value(x):
    """Return the absolute value of a number."""
    return abs(x)

def factorial(n):
    """Return the factorial of a non-negative integer."""
    if n < 0:
        raise ValueError("Factorial is not defined for negative numbers.")
    if n == 0:
        return 1
    result = 1
    for i in range(1, n + 1):
        result *= i
    return result
