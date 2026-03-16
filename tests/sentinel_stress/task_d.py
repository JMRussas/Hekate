import os

def complex_analysis(data):
    # Simulate some complex analysis
    word_count = len(data.split())
    char_count = len(data)
    # More complex simulation
    for i in range(1000):
        for j in range(1000):
            _ = i * j
    return word_count, char_count

file_path = '/tmp/nonexistent_huge_file.txt'

print(f"Task D: Attempting complex analysis of {file_path}")

try:
    with open(file_path, 'r') as f:
        content = f.read()
        word_count, char_count = complex_analysis(content)
        print(f"Analysis successful: {word_count} words, {char_count} characters.")
except FileNotFoundError:
    print(f"Error: File not found at {file_path}. This is an expected failure.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

