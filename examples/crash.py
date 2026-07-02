"""A small program that dies, to demonstrate flight.

Run it directly — it installs the recorder itself:

    python examples/crash.py

On the uncaught ZeroDivisionError, flight writes a .flight file capturing every
frame, its locals, the object graph and the source. Then:

    python -m flight inspect flight-*.flight

You can also record a script you don't want to edit, without the `import flight`:

    python -m flight run some_script.py
"""

import flight

flight.install()


def compute_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def summarize(datasets):
    results = []
    for name, data in datasets.items():
        avg = compute_average(data)
        results.append((name, avg))
    return results


def main():
    datasets = {
        "morning": [10, 20, 30],
        "afternoon": [5, 15],
        "evening": [],  # oops: empty -> ZeroDivisionError in compute_average
    }
    print(summarize(datasets))


if __name__ == "__main__":
    main()
