from app.services.track_naming import normalize_track_name


def test_normalizes_breaking_copyright_quoted_title():
    result = normalize_track_name(
        '📜 Copyright Free Classical Music - "Ephemera" by Scott Buckley 🇦🇺',
        "BreakingCopyright — Royalty Free Music",
    )
    assert result.label == "Scott Buckley - Ephemera"


def test_normalizes_rights_prefix_artist_dash_title_and_version():
    result = normalize_track_name(
        "[No Copyright Music] @ScottBuckley - Stars In Her Skies [Orchestral]",
        "BreakingCopyright — Royalty Free Music",
    )
    assert result.label == "Scott Buckley - Stars In Her Skies"
    assert result.version == "Orchestral"


def test_normalizes_quoted_pop_title_with_apostrophe():
    result = normalize_track_name(
        "'We Won't Tell Nobody' by JOSH LUMSDEN 🇺🇸 | Pop Music With Lyrics (No Copyright) 💌",
        "BreakingCopyright — Royalty Free Music",
    )
    assert result.label == "JOSH LUMSDEN - We Won't Tell Nobody"


def test_normalizes_infraction_slash_title():
    result = normalize_track_name(
        "Rock Sport Racing by Infraction [No Copyright Music] / I Will Run",
        "Infraction - No Copyright Music",
    )
    assert result.label == "Infraction - I Will Run"


def test_normalizes_multi_artist_infraction_title():
    result = normalize_track_name(
        "Phonk Brazilian Afro Sport by Infraction, Emerel Gray [Copyright Free Music] / MONTAGEM MAYE",
        "Infraction - No Copyright Music, EMEREL GRAY",
    )
    assert result.label == "Infraction, Emerel Gray - MONTAGEM MAYE"


def test_normalizes_mokka_rights_before_artist():
    result = normalize_track_name(
        "(No Copyright Music) Slow Fashion Rock [Blues Rock] by MokkaMusic / Slow Tension",
        "MokkaMusic",
    )
    assert result.label == "MokkaMusic - Slow Tension"


def test_normalizes_dash_before_video_marker():
    result = normalize_track_name(
        "Phonk Cyberpunk by Infraction & Lazerpunk- Digiphonk / VIDEO/ [No Copyright Music]",
        "Infraction - No Copyright Music",
    )
    assert result.label == "Infraction & Lazerpunk - Digiphonk"


def test_normalizes_compact_artist_dash_title():
    result = normalize_track_name(
        "[Cyberpunk] Infraction, Emerel Gray, MOKKA- So Lost [No Copyright Music]",
        "Infraction - No Copyright Music",
    )
    assert result.label == "Infraction, Emerel Gray, MOKKA - So Lost"


def test_never_carries_source_rights_seo_into_label():
    result = normalize_track_name(
        "G-House By Alexi Action ( No Copyright Music) / Get Down Low",
        "Infraction - No Copyright Music",
    )
    assert result.label == "Alexi Action - Get Down Low"
    assert "copyright" not in result.label.lower()
    assert "royalty" not in result.label.lower()


def test_removes_free_download_suffix_from_artist():
    result = normalize_track_name(
        "I Can Tell You by Infraction [Future House No Copyright Music] [Free Download]",
        "Infraction - No Copyright Music",
    )
    assert result.label == "Infraction - I Can Tell You"
