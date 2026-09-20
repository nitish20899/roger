"""Holding: the one instruction that must work every time, with no model in the loop."""
from roger.manners import Manners


def m():
    return Manners(["roger", "rodger"])


def test_a_hold_needs_the_name_and_an_instruction_to_stop():
    a = m()
    for said in ["Hold on, Roger", "Roger, mute", "roger hang on a sec", "Wait Roger, let us talk first",
                 "Roger, not now", "Roger, one second", "Roger, give us a minute", "Rodger, stay out of this"]:
        assert a.is_hold(said), said
    for said in ["hold on a second everyone", "Roger, what is the port?", "wait, did the build pass?",
                 "Bob, hold on a moment", "let us wait for Alice"]:
        assert not a.is_hold(said), said


def test_a_hold_lasts_until_the_name_is_used_again():
    a = m()
    assert a.heard("Roger, what did we decide?") is None and not a.holding
    assert a.heard("Hold on, Roger.") == "hold" and a.holding
    # follow-ups during a hold are ignored however direct they are
    for said in ["So what should we do about the deployment?", "Any thoughts?", "What do you reckon?"]:
        assert a.heard(said) is None, said
        assert a.holding
    assert a.heard("Roger, are you back with us?") == "resume"
    assert not a.holding


def test_holding_twice_does_not_re_announce():
    a = m()
    assert a.heard("Roger, mute") == "hold"
    assert a.heard("Roger, be quiet") is None  # already holding
    assert a.holding


def test_no_wake_words_means_nothing_is_a_hold():
    a = Manners([])
    assert not a.named("Roger, mute") and not a.is_hold("Roger, mute")
