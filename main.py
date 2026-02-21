import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
import math
import discord
from discord import app_commands
from typing import Optional

DB_PATH = "bot.db"
GENRES_PATH = "genres_random.txt"


def slugify_for_everynoise(name: str) -> str:
    """
    Convertit un nom de genre en version "sans espaces et sans caractères spéciaux"
    pour construire:
    https://everynoise.com/everynoise1d-{slug}.html
    """
    s = name.strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # enlève accents
    s = re.sub(r"\s+", "", s)           # enlève espaces
    s = re.sub(r"[^a-z0-9]", "", s)     # garde seulement a-z0-9
    return s


def everynoise_url(genre_name: str) -> str:
    slug = slugify_for_everynoise(genre_name)
    return f"https://everynoise.com/everynoise1d-{slug}.html"


def load_genres(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        genres = [line.strip() for line in f if line.strip()]
    if not genres:
        raise RuntimeError("genres_random.txt est vide.")
    return genres


def db_connect():
    return sqlite3.connect(DB_PATH)


def db_init(genres: list[str]):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            user_id INTEGER PRIMARY KEY,
            idx INTEGER NOT NULL DEFAULT 0
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            user_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            score1 INTEGER,
            score2 INTEGER,
            flag1 INTEGER NOT NULL DEFAULT 0,
            flag2 INTEGER NOT NULL DEFAULT 0,
            comment TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, genre_id)
        )
        """)
        # remplir genres si vide
        cur.execute("SELECT COUNT(*) FROM genres")
        (count,) = cur.fetchone()
        if count == 0:
            cur.executemany("INSERT INTO genres(id, name) VALUES(?, ?)",
                            [(i, g) for i, g in enumerate(genres)])
        con.commit()


def get_user_idx(user_id: int) -> int:
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT idx FROM progress WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO progress(user_id, idx) VALUES(?, 0)", (user_id,))
            con.commit()
            return 0
        return int(row[0])


def set_user_idx(user_id: int, idx: int):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO progress(user_id, idx) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET idx=excluded.idx",
                    (user_id, idx))
        con.commit()


def get_rating(user_id: int, genre_id: int):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
        SELECT score1, score2, flag1, flag2, comment, updated_at
        FROM ratings
        WHERE user_id=? AND genre_id=?
        """, (user_id, genre_id))
        return cur.fetchone()


def upsert_rating(user_id: int, genre_id: int,
                  score1=None, score2=None,
                  flag1=None, flag2=None,
                  comment=None):
    now = datetime.now(timezone.utc).isoformat()
    existing = get_rating(user_id, genre_id)
    if existing:
        ex_s1, ex_s2, ex_f1, ex_f2, ex_c, _ = existing
        score1 = ex_s1 if score1 is None else score1
        score2 = ex_s2 if score2 is None else score2
        flag1 = ex_f1 if flag1 is None else flag1
        flag2 = ex_f2 if flag2 is None else flag2
        comment = ex_c if comment is None else comment
    else:
        score1 = score1
        score2 = score2
        flag1 = 0 if flag1 is None else flag1
        flag2 = 0 if flag2 is None else flag2
        comment = comment

    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
        INSERT INTO ratings(user_id, genre_id, score1, score2, flag1, flag2, comment, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, genre_id) DO UPDATE SET
          score1=excluded.score1,
          score2=excluded.score2,
          flag1=excluded.flag1,
          flag2=excluded.flag2,
          comment=excluded.comment,
          updated_at=excluded.updated_at
        """, (user_id, genre_id, score1, score2, int(flag1), int(flag2), comment, now))
        con.commit()


def build_embed(genre_id: int, genre_name: str, user_id: int) -> discord.Embed:
    url = everynoise_url(genre_name)
    e = discord.Embed(title=f"🎧 {genre_name}", description=url)

    r = get_rating(user_id, genre_id)
    if r:
        s1, s2, f1, f2, c, updated_at = r
        e.add_field(name="Skip", value=str(s1) if s1 is not None else "—", inline=True)
        e.add_field(name="Kiff", value=str(s2) if s2 is not None else "—", inline=True)
        e.add_field(name="Special", value="✅" if f1 else "⬜", inline=True)
        e.add_field(name="Flou", value="✅" if f2 else "⬜", inline=True)
        if c:
            e.add_field(name="Commentaire", value=c[:900], inline=False)
        e.set_footer(text=f"Dernière mise à jour: {updated_at}")
    else:
        e.add_field(name="Skip", value="—", inline=True)
        e.add_field(name="Kiff", value="—", inline=True)
        e.add_field(name="Special", value="⬜", inline=True)
        e.add_field(name="Flou", value="⬜", inline=True)
        e.set_footer(text="Pas encore noté")

    return e


class RateModal(discord.ui.Modal, title="Noter ce genre"):
    score1 = discord.ui.TextInput(label="Skip (0-10)", placeholder="ex: 7", required=False, max_length=2)
    score2 = discord.ui.TextInput(label="Kiff (0-10)", placeholder="ex: 9", required=False, max_length=2)
    comment = discord.ui.TextInput(label="Commentaire (optionnel)", required=False, style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, genre_id: int, genre_name: str):
        super().__init__()
        self.genre_id = genre_id
        self.genre_name = genre_name

    async def on_submit(self, interaction: discord.Interaction):
        def parse_score(v: str | None):
            if not v:
                return None
            v = v.strip()
            if not v:
                return None
            n = int(v)
            if n < 0 or n > 10:
                raise ValueError("score hors 0-10")
            return n

        try:
            s1 = parse_score(str(self.score1.value) if self.score1.value is not None else None)
            s2 = parse_score(str(self.score2.value) if self.score2.value is not None else None)
        except Exception:
            await interaction.response.send_message("Notes invalides. Mets des entiers entre 0 et 10.", ephemeral=True)
            return

        upsert_rating(
            user_id=interaction.user.id,
            genre_id=self.genre_id,
            score1=s1,
            score2=s2,
            comment=str(self.comment.value).strip() if self.comment.value else None
        )

        embed = build_embed(self.genre_id, self.genre_name, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=GenreView(self.genre_id, self.genre_name))


class GenreView(discord.ui.View):
    def __init__(self, genre_id: int, genre_name: str):
        super().__init__(timeout=None)
        self.genre_id = genre_id
        self.genre_name = genre_name

    @discord.ui.button(label="📝 Noter", style=discord.ButtonStyle.primary)
    async def rate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RateModal(self.genre_id, self.genre_name))

    @discord.ui.button(label="☑ Special", style=discord.ButtonStyle.secondary)
    async def toggle_flag1(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = get_rating(interaction.user.id, self.genre_id)
        f1 = 0
        if r:
            _, _, ex_f1, _, _, _ = r
            f1 = 0 if ex_f1 else 1
        else:
            f1 = 1
        upsert_rating(interaction.user.id, self.genre_id, flag1=f1)
        embed = build_embed(self.genre_id, self.genre_name, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=GenreView(self.genre_id, self.genre_name))

    @discord.ui.button(label="☑ Flou", style=discord.ButtonStyle.secondary)
    async def toggle_flag2(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = get_rating(interaction.user.id, self.genre_id)
        f2 = 0
        if r:
            _, _, _, ex_f2, _, _ = r
            f2 = 0 if ex_f2 else 1
        else:
            f2 = 1
        upsert_rating(interaction.user.id, self.genre_id, flag2=f2)
        embed = build_embed(self.genre_id, self.genre_name, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=GenreView(self.genre_id, self.genre_name))


class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Synchronise les slash commands (global). Peut prendre quelques minutes à apparaître.
        await self.tree.sync()


genres = load_genres(GENRES_PATH)
db_init(genres)

client = MyClient()


@client.tree.command(name="next", description="Affiche le prochain genre à noter")
async def next_genre(interaction: discord.Interaction):
    user_id = interaction.user.id
    idx = get_user_idx(user_id)
    idx = idx % len(genres)

    genre_id = idx
    genre_name = genres[idx]

    # avance pour le prochain /next
    set_user_idx(user_id, idx + 1)

    embed = build_embed(genre_id, genre_name, user_id)
    await interaction.response.send_message(embed=embed, view=GenreView(genre_id, genre_name), ephemeral=False)


class SearchResultsView(discord.ui.View):
    def __init__(self, user_id: int, results: list[tuple[int, str]], query: str, page_size: int = 15):
        super().__init__(timeout=180)  # 3 minutes
        self.user_id = user_id
        self.results = results
        self.query = query
        self.page_size = page_size
        self.page = 0
        self._update_buttons()

    def _total_pages(self) -> int:
        return max(1, math.ceil(len(self.results) / self.page_size))

    def _page_slice(self):
        start = self.page * self.page_size
        end = start + self.page_size
        return self.results[start:end], start, end

    def _make_embed(self) -> discord.Embed:
        page_items, start, end = self._page_slice()
        total = len(self.results)
        total_pages = self._total_pages()

        e = discord.Embed(
            title=f"🔎 Résultats pour: {self.query}",
            description=f"{total} résultat(s) • Page {self.page + 1}/{total_pages}"
        )

        # Affichage style: "123 — Genre Name"
        lines = [f"`{i}` — {g}" for i, g in page_items]
        e.add_field(name="Genres", value="\n".join(lines) if lines else "—", inline=False)
        return e

    def _update_buttons(self):
        total_pages = self._total_pages()
        self.prev_button.disabled = (self.page <= 0)
        self.next_button.disabled = (self.page >= total_pages - 1)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Empêche les autres de cliquer sur ta pagination
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Cette pagination ne t’est pas destinée.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅ Précédent", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    @discord.ui.button(label="➡ Suivant", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self._total_pages() - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    @discord.ui.button(label="🗑 Fermer", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Recherche fermée.", embed=None, view=None)


@client.tree.command(name="search", description="Cherche des genres par nom")
@app_commands.describe(query="Texte à chercher dans les noms de genres")
async def search_genres(interaction: discord.Interaction, query: str):
    q = query.strip().lower()
    if not q:
        await interaction.response.send_message("La recherche ne peut pas être vide.", ephemeral=True)
        return

    results = [(i, g) for i, g in enumerate(genres) if q in g.lower()]
    if not results:
        await interaction.response.send_message("Aucun genre ne correspond à ta recherche.", ephemeral=True)
        return

    view = SearchResultsView(user_id=interaction.user.id, results=results, query=query, page_size=15)
    await interaction.response.send_message(embed=view._make_embed(), view=view, ephemeral=True)


@client.tree.command(name="info", description="Affiche les infos d'un genre par son ID")
@app_commands.describe(genre_id="ID du genre (voir /search)")
async def info_genre(interaction: discord.Interaction, genre_id: int):
    if genre_id < 0 or genre_id >= len(genres):
        await interaction.response.send_message("ID de genre invalide.", ephemeral=True)
        return

    genre_name = genres[genre_id]
    embed = build_embed(genre_id, genre_name, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=GenreView(genre_id, genre_name), ephemeral=False)

def computed_score(skip: Optional[int], kiff: Optional[int], special: int, flou: int) -> Optional[float]:
    """
    Renvoie la note calculée selon tes règles.
    - special=1 => exclu => None
    - flou=1 => 0.35*skip + 0.65*kiff
    - sinon => 0.5*skip + 0.5*kiff
    Si skip ou kiff manquent => None
    """
    if special:
        return None
    if skip is None or kiff is None:
        return None
    if flou:
        return 0.35 * skip + 0.65 * kiff
    return 0.5 * skip + 0.5 * kiff


def fetch_user_rows(user_id: int):
    """
    Récupère toutes les lignes ratings de l'utilisateur
    """
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
        SELECT genre_id, score1, score2, flag1, flag2, comment, updated_at
        FROM ratings
        WHERE user_id = ?
        """, (user_id,))
        return cur.fetchall()
    
@client.tree.command(name="stats", description="Stats sur tes notes (en excluant Special)")
async def stats(interaction: discord.Interaction):
    user_id = interaction.user.id
    rows = fetch_user_rows(user_id)

    total_rows = len(rows)
    special_count = 0
    eligible_count = 0
    flou_count = 0
    scored_count = 0

    scores = []
    skips = []
    kiffs = []

    # Pour top/bottom
    scored_items = []  # (score, genre_id, genre_name)

    for (genre_id, s1, s2, f1, f2, _comment, _updated) in rows:
        if f1:  # Special => exclu
            special_count += 1
            continue

        eligible_count += 1
        if f2:
            flou_count += 1

        sc = computed_score(s1, s2, f1, f2)
        if sc is None:
            continue

        scored_count += 1
        scores.append(sc)
        skips.append(s1)
        kiffs.append(s2)
        scored_items.append((sc, genre_id, genres[genre_id]))

    e = discord.Embed(title="📊 Stats")

    e.add_field(name="Entrées totales (ratings)", value=str(total_rows), inline=True)
    e.add_field(name="Exclus (Special)", value=str(special_count), inline=True)
    e.add_field(name="Pris en compte (non Special)", value=str(eligible_count), inline=True)

    e.add_field(name="Cochés Flou (dans non Special)", value=str(flou_count), inline=True)
    e.add_field(name="Notés (Skip+Kiff présents)", value=str(scored_count), inline=True)

    if scored_count > 0:
        avg_score = sum(scores) / scored_count
        avg_skip = sum(skips) / scored_count
        avg_kiff = sum(kiffs) / scored_count

        e.add_field(name="Moyenne note calculée", value=f"{avg_score:.2f}", inline=True)
        e.add_field(name="Moyenne Skip", value=f"{avg_skip:.2f}", inline=True)
        e.add_field(name="Moyenne Kiff", value=f"{avg_kiff:.2f}", inline=True)

        # Top 5 / Bottom 5
        scored_items.sort(key=lambda x: x[0], reverse=True)
        top5 = scored_items[:5]
        bot5 = scored_items[-5:][::-1]

        top_lines = [f"**{sc:.2f}** — `{gid}` {name}" for sc, gid, name in top5]
        bot_lines = [f"**{sc:.2f}** — `{gid}` {name}" for sc, gid, name in bot5]

        e.add_field(name="🏆 Top 5", value="\n".join(top_lines) if top_lines else "—", inline=False)
        e.add_field(name="🧊 Bottom 5", value="\n".join(bot_lines) if bot_lines else "—", inline=False)
    else:
        e.add_field(name="Moyenne note calculée", value="— (pas assez de notes)", inline=False)

    await interaction.response.send_message(embed=e, ephemeral=True)

class RankOrderSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Décroissant (meilleurs d'abord)", value="desc", emoji="⬇️"),
            discord.SelectOption(label="Croissant (pires d'abord)", value="asc", emoji="⬆️"),
        ]
        super().__init__(placeholder="Choisis l'ordre du classement…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: RankView = self.view  # type: ignore
        view.order = self.values[0]
        view.page = 0
        view._update_buttons()
        await interaction.response.edit_message(embed=view._make_embed(), view=view)


class RankView(discord.ui.View):
    def __init__(self, user_id: int, items: list[tuple[float, int, str]]):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.items = items  # (score, genre_id, name) -> déjà filtrés "non Special" et scorés
        self.order = "desc"
        self.page = 0
        self.page_size = 15

        self.select = RankOrderSelect()
        self.add_item(self.select)

        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ce classement ne t’est pas destiné.", ephemeral=True)
            return False
        return True

    def _sorted_items(self):
        reverse = (self.order == "desc")
        return sorted(self.items, key=lambda x: x[0], reverse=reverse)

    def _total_pages(self) -> int:
        return max(1, math.ceil(len(self.items) / self.page_size))

    def _page_slice(self):
        sitems = self._sorted_items()
        start = self.page * self.page_size
        end = start + self.page_size
        return sitems[start:end], start, end, len(sitems)

    def _make_embed(self) -> discord.Embed:
        page_items, start, end, total = self._page_slice()
        total_pages = self._total_pages()

        title = "📈 Classement (note calculée)"
        subtitle = f"{total} genre(s) • Page {self.page + 1}/{total_pages} • Ordre: {'Décroissant' if self.order=='desc' else 'Croissant'}"
        e = discord.Embed(title=title, description=subtitle)

        if not page_items:
            e.add_field(name="Résultats", value="—", inline=False)
            return e

        lines = []
        rank_start = start + 1
        for idx, (sc, gid, name) in enumerate(page_items):
            lines.append(f"**#{rank_start + idx}** — **{sc:.2f}** — `{gid}` {name}")

        e.add_field(name="Genres", value="\n".join(lines), inline=False)
        return e

    def _update_buttons(self):
        total_pages = self._total_pages()
        self.prev_button.disabled = (self.page <= 0)
        self.next_button.disabled = (self.page >= total_pages - 1)

    @discord.ui.button(label="⬅ Page", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    @discord.ui.button(label="Page ➡", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self._total_pages() - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    @discord.ui.button(label="🗑 Fermer", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Classement fermé.", embed=None, view=None)

@client.tree.command(name="rank", description="Classement des genres (note calculée, Special exclus)")
async def rank(interaction: discord.Interaction):
    user_id = interaction.user.id
    rows = fetch_user_rows(user_id)

    items = []
    for (genre_id, s1, s2, f1, f2, _comment, _updated) in rows:
        sc = computed_score(s1, s2, f1, f2)
        if sc is None:
            continue  # exclut Special + ceux sans 2 notes
        items.append((sc, genre_id, genres[genre_id]))

    if not items:
        await interaction.response.send_message(
            "Aucun genre classable pour l’instant.\n"
            "- Les genres **Special** sont exclus\n"
            "- Il faut **Skip + Kiff** pour calculer une note",
            ephemeral=True
        )
        return

    view = RankView(user_id=user_id, items=items)
    await interaction.response.send_message(embed=view._make_embed(), view=view, ephemeral=True)

def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN manquant. Mets-le en variable d'environnement.")
    client.run(token)


if __name__ == "__main__":
    main()