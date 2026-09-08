from roger.text import norm_text, speech_clean, take_sentences


def test_take_sentences_splits_completed_sentences_only():
    sentences, rest = take_sentences("It listens on port eight. You can change it in the env file. And then")
    assert sentences == ["It listens on port eight.", "You can change it in the env file."]
    assert rest == "And then"


def test_take_sentences_keeps_abbreviations_and_numbered_items_together():
    sentences, rest = take_sentences("Use e.g. the second option. Step 1. Open the file")
    assert sentences == ["Use e.g. the second option."]
    assert rest == "Step 1. Open the file"


def test_speech_clean_strips_markdown():
    assert speech_clean("Run `make run`, then **wait**.\n\n# Done") == "Run make run, then wait. Done"


def test_norm_text():
    assert norm_text("Hi, everyone!  I'm Roger.") == "hi everyone i m roger"
