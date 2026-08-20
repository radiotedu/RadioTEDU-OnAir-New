"""
Music History Database
Provides "This Day in Music History" events for AI commentary enrichment.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.runtime_paths import get_data_dir


class MusicHistoryDB:
    """SQLite database of daily music history events."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(get_data_dir() / "music_history.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history_events (
                    day_of_year INTEGER PRIMARY KEY,
                    date_str TEXT,
                    title TEXT,
                    description TEXT,
                    category TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS composer_anniversaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    composer_name TEXT NOT NULL,
                    birth_date TEXT,
                    death_date TEXT,
                    notable_works TEXT,
                    birth_year INTEGER,
                    death_year INTEGER
                )
            """)
            conn.commit()

            # Seed if empty
            cur = conn.execute("SELECT COUNT(*) FROM history_events")
            if cur.fetchone()[0] == 0:
                self._seed_events(conn)
        finally:
            conn.close()

    def _seed_events(self, conn):
        """Seed database with classical music history events."""
        events = [
            (1, "January 1", "New Year's Day in Music", "Many composers wrote celebratory music for the new year, including Handel's Music for the Royal Fireworks.", "milestone"),
            (17, "January 17", "Handel's Birthday", "George Frideric Handel was born on February 23, 1685. His Messiah remains a cornerstone of classical repertoire.", "birth"),
            (32, "February 1", "Schubert's Death", "Franz Schubert died on November 19, 1828, leaving behind over 600 lieder and 9 symphonies.", "death"),
            (60, "February 29", "Rossini's Leap Year", "Gioachino Rossini was born on February 29, 1792. His operas include The Barber of Seville and William Tell.", "birth"),
            (75, "March 16", "Bach's Well-Tempered Clavier", "Bach completed Book I of The Well-Tempered Clavier in 1722, a landmark in keyboard music.", "milestone"),
            (100, "April 10", "Beethoven's Premiere", "Beethoven's Symphony No. 3 'Eroica' premiered in 1805, revolutionizing the symphony form.", "premiere"),
            (121, "May 1", "Mahler's Symphony No. 1", "Gustav Mahler's Symphony No. 1 'Titan' premiered in Budapest in 1889.", "premiere"),
            (152, "June 1", "Stravinsky's Rite of Spring", "Stravinsky's The Rite of Spring caused a riot at its 1913 Paris premiere.", "premiere"),
            (172, "June 21", "Summer Solstice Music", "Many composers wrote music celebrating light and nature, including Smetana's The Moldau.", "milestone"),
            (200, "July 19", "Chopin's Nocturnes", "Frédéric Chopin's nocturnes redefined piano music with their poetic intimacy.", "milestone"),
            (230, "August 18", "Mozart's Death", "Wolfgang Amadeus Mozart died on December 5, 1791, at age 35, leaving his Requiem unfinished.", "death"),
            (260, "September 17", "Beethoven's Birthday", "Ludwig van Beethoven was baptized on December 17, 1770. His nine symphonies changed music forever.", "birth"),
            (290, "October 17", "Chopin's Death", "Frédéric Chopin died on October 17, 1849, in Paris at age 39.", "death"),
            (320, "November 16", "Beethoven's 5th", "Beethoven's Symphony No. 5 premiered in Vienna in 1808 on a marathon concert.", "premiere"),
            (350, "December 16", "Beethoven's Birth", "Ludwig van Beethoven was baptized on December 17, 1770, in Bonn, Germany.", "birth"),
            (359, "December 25", "Christmas Music", "Handel's Messiah, Corelli's Christmas Concerto, and Bach's Christmas Oratorio are holiday staples.", "milestone"),
        ]

        conn.executemany(
            "INSERT INTO history_events (day_of_year, date_str, title, description, category) VALUES (?, ?, ?, ?, ?)",
            events,
        )

        composers = [
            ("Johann Sebastian Bach", "1685-03-21", "1750-07-28", "Brandenburg Concertos, Well-Tempered Clavier, Mass in B Minor", 1685, 1750),
            ("Wolfgang Amadeus Mozart", "1756-01-27", "1791-12-05", "Symphony No. 40, Requiem, The Marriage of Figaro", 1756, 1791),
            ("Ludwig van Beethoven", "1770-12-17", "1827-03-26", "Symphony No. 9, Moonlight Sonata, Fidelio", 1770, 1827),
            ("Frédéric Chopin", "1810-02-22", "1849-10-17", "Nocturnes, Ballades, Piano Concerto No. 1", 1810, 1849),
            ("Johannes Brahms", "1833-05-07", "1897-04-03", "Symphony No. 4, German Requiem, Hungarian Dances", 1833, 1897),
            ("Pyotr Ilyich Tchaikovsky", "1840-05-07", "1893-11-06", "Swan Lake, 1812 Overture, Symphony No. 6", 1840, 1893),
            ("Gustav Mahler", "1860-07-07", "1911-05-18", "Symphony No. 2 'Resurrection', Das Lied von der Erde", 1860, 1911),
            ("Igor Stravinsky", "1882-06-17", "1971-04-06", "The Rite of Spring, The Firebird, Petrushka", 1882, 1971),
        ]

        conn.executemany(
            "INSERT INTO composer_anniversaries (composer_name, birth_date, death_date, notable_works, birth_year, death_year) VALUES (?, ?, ?, ?, ?, ?)",
            composers,
        )

        conn.commit()

    def get_today_event(self) -> Optional[dict]:
        """Get the music history event for today."""
        day_of_year = datetime.now().timetuple().tm_yday
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM history_events WHERE day_of_year = ?",
                (day_of_year,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def get_composer_anniversary(self, composer_name: str) -> Optional[dict]:
        """Check if a composer has an anniversary today."""
        now = datetime.now()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM composer_anniversaries WHERE composer_name = ?",
                (composer_name,),
            )
            row = cur.fetchone()
            if row:
                data = dict(row)
                # Check if today is birth or death date
                if data.get("birth_date"):
                    birth = datetime.strptime(data["birth_date"], "%Y-%m-%d")
                    if birth.month == now.month and birth.day == now.day:
                        data["anniversary_type"] = "birth"
                        data["anniversary_years"] = now.year - birth.year
                        return data
                if data.get("death_date"):
                    death = datetime.strptime(data["death_date"], "%Y-%m-%d")
                    if death.month == now.month and death.day == now.day:
                        data["anniversary_type"] = "death"
                        data["anniversary_years"] = now.year - death.year
                        return data
            return None
        finally:
            conn.close()
