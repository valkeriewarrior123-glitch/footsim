# Football Simulator

Personal alternate-history football simulator built around one created player.
See the project charter for the rules (canon locks at 2001-01-01; only matches
involving the player's teams change).

## Layout

```
footsim/
  footsim/
    schema.sql     database tables (baseline, rules, strength, timeline)
    db.py          create the database, load rulesets
    import_statsbomb.py  load a StatsBomb season into the baseline
    validate.py    data consistency checks
    timeline.py    branches, overrides, the "active" view of an edition
    engine.py      group tables, bracket, suspensions, invalidation cascade
    stats.py       player/team stats, awards, records, career totals
    ratings.py     Elo ratings from every international result + goal model
    roller.py      single-match generator
    player.py      the created player: profile, squad swap, roles, lineup rule
    api.py         editor operations (edit, revert, diff, branches)
    server.py      local web editor (standard library only)
    web/index.html the editor page
  data_packs/
    WC2022/
      ruleset.json tournament rules + bracket for World Cup 2022
  tests/
    test_schema.py checks the schema and canon rule
    test_engine.py gate test, cascade, forks, suspensions
```

## Engine

```
python -m footsim.engine WC2022        # baseline tables, bracket, champion
python -m footsim.engine WC2022 3      # same, as seen in branch 3
```

Knockout slots are `valid` (has a result for the right teams), `pending`
(teams known, result needed) or `tbd` (waiting on an earlier slot).
`apply_cascade()` stores pending slots in the branch, or fills them from a real
knockout meeting of the same two teams in the same competition, if one exists.

## Setup

Requires Python 3.10+ (no extra packages yet).

```
python -m footsim.db          # creates footsim.db
python -m unittest discover tests
```

## Data layers

| Layer    | Tables | Edited by user? |
|----------|--------|-----------------|
| Baseline | competitions, editions, teams, players, squads, fixtures, lineups, match_events, awards, calendar_events | Never |
| Rules    | rulesets (JSON per edition) | Custom mode only |
| Strength | team_ratings (Elo by date), historical_finishes | No |
| Timeline | branches, squad/fixture/event/lineup overrides, injuries, career events, displacement_log | Yes |

A branch = baseline + its ordered overrides. Forks inherit the parent's
overrides up to `fork_seq`.

## Import World Cup 2022

```
git clone --depth 1 --filter=blob:none --sparse https://github.com/statsbomb/open-data
python -m footsim.import_statsbomb path/to/open-data/data
python -m footsim.validate WC2022
```

France, Iran and Uruguay show 25 players: StatsBomb only lists players named in a matchday squad (France really did play with 25 after Benzema left).

## Stats and awards

```
python -m footsim.stats WC2022         # scorers, awards, finishes, records
python -m footsim.stats WC2022 3       # same, in branch 3
```

Everything is counted from match events, using only results that stand in the
branch (pending knockout matches count for nothing). The Golden Boot is
recalculated (goals, then assists, then fewer minutes). Voted awards (Golden
Ball, Golden Glove, Best Young) keep their real winners unless you set them in
a branch with `stats.set_award()`.

## Editor

```
python -m footsim.server          # opens http://localhost:8765
```

1. Pick **New branch** (real history is read-only).
2. Click any match, change the score and the goal/card events, then **Save & recalculate**.
   The score must match the goal events; drawn knockout matches need extra time and a shootout winner.
3. A **What changed** summary shows new group order, knockout matches now needing a result,
   the champion, team finishes, the Golden Boot and player stat swings.
4. Matches marked **needs result** were created by your change. Fill them the same way.
5. **Revert to history** on any edited match undoes it. **Fork** copies a branch to try alternatives.

## Team strength and the roller

```
git clone --depth 1 https://github.com/martj42/international_results
python -m footsim.ratings international_results/results.csv
```

This computes World Football Elo ratings (eloratings.net method) for every
national team from every international since 1872, stores each team's rating
since 2000, and fits the goal model (`data_packs/goal_model.json`) on 14,000+
competitive matches from 2000-2022.

The roller uses each team's Elo going into the tournament (no hindsight),
Poisson goals, extra time and a kick-by-kick shootout for knockout draws,
then picks scorers, assists, penalties, own goals and cards from the players on
the pitch, weighted by position and real scoring rate. Suspended players are
left out of the lineup.

In the editor: **Roll** in a match panel fills in a generated result you can
edit, reroll or save. **Roll remaining matches** generates every match still
needing a result until there's a champion. Rolled matches are tagged "rolled".

## Your created player

In a branch, open **My player** and create him: name, birth date, national
team, position, tier (squad player 65, starter 75, star 83, world-class 89,
generational 94) and an overall rating you can fine-tune. Pick a real squad
member he replaces; that player leaves the squad.

- **Roles.** Set his role in every match: take the replaced player's spot,
  start in place of a named player, come on as a sub at a minute for a named
  player, or not play. Unset matches are flagged "not set" (he takes the
  replaced player's spot if that player really played, otherwise sits out).
- **The lineup rule.** Whoever takes a lineup spot takes that spot's real
  events. Start him in Messi's place in the final and Messi's goals there are
  his; the real result stands until you edit or roll the match.
- **Strict ripple.** Only his national team's matches can be edited. Other
  matches change through the cascade; other teams' new matches can only be rolled.
- **Rolls.** His rating adds Elo to his team for the minutes he plays
  (world-class is about +42 per 90) and sets how often he scores and assists.
- **Injuries and calendar.** Injured dates keep him out. The calendar shows the
  real tournament dates plus anything you add (camps, friendlies, notes).
- **Forks** copy the player, his roles, injuries and calendar.

## Update: feedback round 1

- **Positions and birth dates for every player**, including those who never
  played, from FIFA's official squad lists (`data_packs/WC2022/squad_positions.csv`,
  matched by team and shirt number). Applied automatically when the database opens.
- **Player lists are alphabetical.**
- **Edit your player any time** (My player > Edit player): name, birth date,
  position, tier, rating, who he replaces. Only the national team is fixed.
- **"In place of" defaults to a same-position starter**, shown by name; the
  list puts same-position players first.
- **Suspensions carry forward.** After any change, a player banned for a later
  match is swapped out (the best player at his position takes his spot and its
  events). Real results keep their score and are tagged "lineup fixed". Undo
  the change and the swap is undone. Suspended players can't be picked.
- **Locks.** In the match panel, lock the score or any event (padlock), then
  Roll: only unlocked parts change. A locked player is brought on before his event.
- **Set outcome.** Pick the score and your player's goals; Preview lists every
  goal: real goals kept, removed, or taken by your player, and new goals.
  Save & recalculate confirms.
- **Wider match panel; bracket connector lines; 🥇🥈🥉.**
- **Changes tab** compares the branch with real history: champion, group order,
  knockouts, finishes, Golden Boot, player stat swings, every changed match,
  and your player against the replaced player's real tournament.

Existing databases are upgraded on startup (new tables, positions, and the
suspension rule applied to every saved branch).

## Update: feedback round 2

- **Roles.** Every real player's detailed roles (ST, LW, CAM, CB, ...) come from
  where he actually played (`data_packs/WC2022/player_roles.csv`). Your player
  gets role chips (pick several). "In place of" prefers a starter who plays one
  of his roles.
- **Choosing who he replaces** shows each player's position, roles and minutes,
  with a "didn't play" tag and a filter for players who never played.
- **Set outcome fills everything**: assists for rolled goals, an optional
  target for your player's assists, and cards when the match has none. Real
  goals keep their real assists.
- **Match ratings** (3-10) for every player in every match, shown in the match
  panel, Stats and My player. In a branch, the Golden Ball, Golden Glove and
  Best Young Player are calculated from them once the final is played. With
  nothing changed they pick the real winners (Messi, Martínez, Enzo Fernández).
- **Focus / Free mode** per branch (switch in the banner). Free mode lets you
  hand-edit any match.
- **Bracket picker** (Knockouts): "Pick winners" then click a team; or
  "Choose podium" to set champion, runner-up and third. Real results are kept
  wherever they still fit; impossible podiums are explained.
- **Changes tab** now covers the whole world: a summary, then one collapsible
  section per tournament with the big changes, links to open it, and "Show
  every change" for the full detail.
