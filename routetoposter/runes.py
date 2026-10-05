"""English text written in Anglo-Saxon runes (the futhorc), as on Thrór's Map in The Hobbit.

One rune per letter, with single runes for TH and NG. Runes have no digits, so numbers are written
out as words first (`number_words`).
"""
RUNE = {"A": "ᚪ", "B": "ᛒ", "C": "ᚳ", "D": "ᛞ", "E": "ᛖ", "F": "ᚠ", "G": "ᚷ", "H": "ᚻ", "I": "ᛁ", "J": "ᛄ",
        "K": "ᚳ", "L": "ᛚ", "M": "ᛗ", "N": "ᚾ", "O": "ᚩ", "P": "ᛈ", "Q": "ᚳ", "R": "ᚱ", "S": "ᛋ", "T": "ᛏ",
        "U": "ᚢ", "V": "ᚠ", "W": "ᚹ", "X": "ᛉ", "Y": "ᚣ", "Z": "ᛋ"}
PAIRS = {"TH": "ᚦ", "NG": "ᛝ"}
ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
        "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def to_runes(text: str) -> str:
    """'Spiti Valley' -> 'ᛋᛈᛁᛏᛁ ᚠᚪᛚᛚᛖᚣ'. Spaces and punctuation are kept; letters without a rune
    (accented ones, other scripts) are left out."""
    upper, out, i = text.upper(), [], 0
    while i < len(upper):
        pair = upper[i:i + 2]
        if pair in PAIRS:
            out.append(PAIRS[pair])
            i += 2
            continue
        ch = upper[i]
        if ch in RUNE:
            out.append(RUNE[ch])
        elif not ch.isalnum():
            out.append(ch)
        i += 1
    return "".join(out)


def number_words(n: int) -> str:
    """0..999 as English words: 10 -> 'ten', 42 -> 'forty-two', 833 -> 'eight hundred thirty-three'."""
    if n < 20:
        return ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return TENS[tens] + (f"-{ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{ONES[hundreds]} hundred" + (f" {number_words(rest)}" if rest else "")
    return str(n)
