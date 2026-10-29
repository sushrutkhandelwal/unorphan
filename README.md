# Unorphan

Finds a home for the orphaned files in your Downloads folder.

![Unorphan showing twelve downloads grouped under the projects they belong to](docs/sorter.png)

If you work on a few projects at once, your Downloads folder is where their files pile up: the
podcast episode you exported, a client's brand guidelines, a CSV from some dashboard, a screenshot
called `Screenshot 2026-10-12 at 14.02.11.png`. Each one belongs to a project, but nobody files them
away, so after a month you can't find anything.

Unorphan looks at everything loose in the folder, works out which of your projects each
item belongs to, and moves it into a folder for that project. It already knows your projects: it
reads the README or CLAUDE.md in each of your code folders. You can also add folders of your own
for things broader than one project, like a company you work with.

Nothing moves until you've seen the list and pressed Move, and one click puts it all back.

## How it helps

- **It knows your projects without being told.** Point it at the folder your code lives in. Every
  project in there becomes a destination, described by its own README.
- **It reads pictures, not just names.** That screenshot of a trail map goes to the hiking app,
  even though its name says nothing.
- **It leaves things alone when unsure.** Tax returns, installers, holiday photos: anything that
  doesn't clearly belong somewhere stays where it is.
- **It's easy to undo.** Every move is logged, and **Undo last move** reverses the most recent batch.

The sorting is done by [Jev](https://docs.typesafe.ai/introduction), a model from TypeSafe that
makes classification decisions and says how sure it is. A full run over a couple of hundred files
costs a few cents.

## Getting started

You'll need:

- macOS or Linux with Python 3.11 or newer (Windows should work, but hasn't been tried)
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs everything else
- a TypeSafe API key, from [console.typesafe.ai/keys](https://console.typesafe.ai/keys)

Clone this repository, then from inside it:

```sh
uv tool install .
unorphan ui
```

The first command installs an `unorphan` command you can run from anywhere. The second
opens the app in your browser. The first time, it opens **Settings** and tells you what's missing
(usually the API key).

To try it without installing, run `uv run unorphan ui` from inside the repository instead.

## Using it

**1. Check the settings.** Click **Settings** at the top right.

![The settings panel](docs/settings.png)

- **Folder to organize** is the messy folder. It's `~/Downloads` unless you change it.
- **Where sorted files go** is where the project folders get made. The default is a folder called
  `projects` inside the one you're organizing.
- **Where your projects are** lists the folders that hold your code. Each one shows how many
  projects it found. If yours live somewhere unusual, add that folder here. On a Mac, **Choose…**
  opens the normal folder picker.
- **Jev API key**: paste your key, or point to a `.env` file that has it.

Click **Save**. The app looks through the folder and asks Jev about each file. This takes a few
seconds for a few hundred files.

**2. Add folders of your own (optional).** Some files belong to something bigger than a single
project: a client, your company, a side business. Click **+ New folder** on the left, give it a
name, and describe what goes in it. Jev reads the description, so be specific:

> Northwind Studio, the design agency I freelance for: contracts and MSAs, brand guidelines,
> client decks, briefs and anything else from or about Northwind.

When both a specific project and one of your broader folders could fit a file, Jev picks the
project.

**3. Review the list.** Results come in three groups:

- **Ready to move.** Jev is confident (80% or more by default). These are ticked already and
  grouped by where they're going.
- **Needs a look.** Jev has a guess but isn't sure. Tick the ones you agree with.
- **No match.** Nothing fitted. Leave them, or pick a folder by hand.

You can change the destination of any file with its dropdown, and untick anything you'd rather
keep where it is. Click an image's thumbnail to see it larger.

**4. Move.** Press **Move** at the bottom. The files go into `projects/<folder>/` and disappear
from the list.

**5. Changed your mind?** **Undo last move** puts the most recent batch back exactly where it was.

Run it again whenever the folder fills up. Files Jev has already looked at are remembered, so a
rescan only pays for new ones.

### Getting better results

- **Describe your own folders well.** A list of the kinds of files that belong there works better
  than a one-word name.
- **Give quiet projects a hint.** If a project has no README, open **Details** next to it in the
  Projects list and write a sentence about what it is.
- **Switch off projects that never get files**, like tools or SDKs you've cloned, by unticking them.
- **Move the slider.** *Auto-select at* changes how sure Jev must be before a file is ticked for
  you. Higher is more cautious.

## Command line

Everything also works from a terminal, which is handy for a quick tidy or a scheduled job:

```sh
unorphan                  # show what would move; moves nothing
unorphan --apply          # move it
unorphan --undo           # put back the last move
unorphan --list-projects  # every destination, and what Jev is told about it
```

| Option | What it does |
|---|---|
| `--downloads PATH` | Organize a different folder for this run |
| `--projects-root PATH` | Look for projects here (repeat for more than one) |
| `--threshold 0.9` | How sure Jev must be before a file moves |
| `--no-images` | Send file names only, never pictures |
| `--only '*.pdf'`, `--limit 20` | Try it on a handful of files first |
| `--json` | Output for scripts |
| `ui --port 8080 --no-browser` | Run the app on a fixed port without opening a tab |

## How it decides

For every loose item, Jev gets:

- its name, kind, size and date,
- for folders and `.zip` files, the names of what's inside,
- for images, a small copy of the picture,

and a list of destinations: one per project (its name plus a few sentences from its README,
CLAUDE.md, AGENTS.md or package.json), plus the folders you added. Jev picks one, or none. The
question is asked twice in the same request with the list in opposite orders, and a file only
counts as ready if both answers agree. That stops a destination winning just because it was listed
first.

## Privacy

What leaves your computer goes to TypeSafe's API, and only this:

- the names of your projects and a short description of each, plus your own folders' descriptions
- the names, sizes and dates of the items being sorted, and the names of files inside folders and
  zips
- for images, a copy shrunk to 768 pixels on its longest side and re-compressed, with its metadata
  removed. Never the original.

The contents of documents, PDFs, spreadsheets and text files are never opened. Turn pictures off in
Settings to send names only, or list patterns under *Never touch files matching* to keep certain
files out entirely.

The app itself runs on your computer and only answers requests from it. Each time it starts it
makes a one-time code that the page has to send back, so other websites you have open can't use it.

## Cost

Jev charges by the amount of text it reads: $0.042 per million tokens. With about 40 destinations,
each file uses around 7,500 tokens, so sorting 200 files costs about 7 cents. Answers are
remembered, so running it again only costs anything for new files, or after you change your
folders. Each run shows what it used.

## Where things are kept

| File | What's in it |
|---|---|
| `~/.config/unorphan/config.toml` | Your settings. Only your user account can read it, since it can hold your key. |
| `~/.config/unorphan/folders.json` | Your own folders, project hints and switched-off projects |
| `~/.cache/unorphan/cache.json` | Jev's past answers. Safe to delete. |
| `<sorted files folder>/.unorphan-log.jsonl` | Every move, so it can be undone |

All of them are plain text. Settings can be edited by hand, and
[`config.example.toml`](config.example.toml) lists every option.

## Uninstalling

```sh
uv tool uninstall unorphan
rm -rf ~/.config/unorphan ~/.cache/unorphan
```

Your sorted files stay where they are.

## Working on it

```
unorphan/
  core.py    settings, finding projects, asking Jev, moving and undoing
  cli.py     the unorphan command
  ui.py      the local web server behind the app
  ui.html    the app itself (one file, no build step)
```

`uv run unorphan ui` runs it from a checkout. Jev's API is documented at
[docs.typesafe.ai](https://docs.typesafe.ai/introduction).
