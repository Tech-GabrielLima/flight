"""A small program that dies, to demonstrate flight."""


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
        "evening": [],  # oops: empty -> ZeroDivisionError
    }
    print(summarize(datasets))


if __name__ == "__main__":
    main()
