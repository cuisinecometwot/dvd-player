from domain.cd import track_after, track_before

TRACKS = [1, 2, 3, 4]


def test_track_after():
    assert track_after(TRACKS, 2) == 3
    assert track_after(TRACKS, 4) is None
    assert track_after([2, 5], 2) == 5


def test_track_before_restarts_unless_near_start():
    assert track_before(TRACKS, 3, 10_000) == 3
    assert track_before(TRACKS, 3, 1_000) == 2
    assert track_before(TRACKS, 1, 0) == 1
