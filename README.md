# Citator: Harvard Manchester

Paste a link and get a reference formatted to the University of Manchester Library's
[Harvard Manchester guide](https://subjects.library.manchester.ac.uk/referencing/harvardlist).

## Start it

Double-click **Citator.app**. It opens in your browser at http://127.0.0.1:8765.
You can drag the app to your Dock or Applications folder.

The app runs a small background server from `~/Library/Application Support/Citator`. The server
stops when you log out or restart, and the app starts it again next time.
Problems are logged in `~/Library/Logs/Citator.log`.

(Or run `python3 server.py` in this folder. You only need Python 3, with no extra installs.)

## Use it

- **Paste anywhere** (⌘V) to cite a link right away. You can also type a **DOI** or an **ISBN**.
- **⏎** copies the reference. Italics are kept when you paste into Word or Google Docs.
- **A** adds the reference to your **reference list**. The list is sorted alphabetically,
  adds 2025a/2025b when an author has two sources from the same year, and has **Copy all**.
- **Paste several links at once** (one per line) to build the whole list in one go.
- Use **Edit details** to fix or fill in anything. The reference updates as you type.
  Changing the source type (for example from Webpage to Report) reformats it.
- **Settings** (gear icon):
  - sentence-case titles (on by default, as the guide asks)
  - two-digit days ("07 June")
  - the font used when pasting

## What it handles

| Source | Where the data comes from |
|---|---|
| Journal articles, chapters, conference papers (any DOI, ScienceDirect, Nature, JSTOR, SSRN, publisher pages) | Crossref / DOI record |
| News (BBC, Guardian, FT, Forbes and others) | Page metadata (author, date, newspaper) |
| Webpages, organisation pages, GOV.UK | Page metadata. The organisation is used as the author when no person is named |
| Blogs (Medium, Substack, WordPress and others) | Page metadata |
| YouTube, X/Twitter, TikTok | oEmbed, plus the exact post time read from the post ID |
| Wikipedia | Revision history (the "last updated" year) |
| PDFs and reports | PDF metadata and cover-page text |
| arXiv preprints | arXiv API |
| Books (ISBN, Google Books links) | Open Library / Google Books |

Anything the Citator can't verify appears under **Worth a quick check**. Missing pieces show as
red **[placeholders]**. Sites that block automated reading (such as the FT and The Economist)
fall back to whatever the link itself shows, and you're told which parts to fill in.

## Put it online for others

The folder is ready to host on [Render](https://render.com), which has a free plan.
Upload these files to a GitHub repository: `server.py`, `index.html`, `Dockerfile`,
`render.yaml` and `README.md`. Then go to Render and choose **New → Blueprint**, pick the repository and deploy.

When it runs online (Render sets `$PORT`), the server:
- refuses links to private or internal addresses
- limits each visitor to 40 citations a minute
- never opens a browser
