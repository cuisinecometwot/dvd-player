from domain.cdtext import parse_cd_info

REPORT = """CD-ROM Track List (1 - 2)
  1: 00:02:00  000000 audio  false  no    2        no
__________________________________
CD Analysis Report
CD-TEXT for Disc:
Language 0 'Japanese':
\tTITLE: 理論武装して
\tPERFORMER: 初星学園
\tGENRE: Pop
CD-TEXT for Track  1:
\tTITLE: 理論武装して
\tPERFORMER: 雨夜燕
\tTITLE: second language title
CD-TEXT for Track  2:
\tTITLE: 
__________________________________
\tTITLE: not cd-text
"""


def test_parse_cd_text():
    info = parse_cd_info(REPORT, [1, 2])
    assert (info.title, info.artist) == ("理論武装して", "初星学園")
    assert (info.track(1).title, info.track(1).artist) == ("理論武装して", "雨夜燕")
    assert info.track(2).title == "Track 02"


def test_no_cd_text():
    assert parse_cd_info("CD Analysis Report\nNo CD-TEXT on Disc.\n", [1, 2]) is None
    assert parse_cd_info("CD-TEXT for Disc:\n\tGENRE: Pop\n", [1]) is None
