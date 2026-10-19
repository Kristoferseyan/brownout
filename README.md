# brownout

My power company posts its scheduled power interruptions as images on its WordPress site. I got tired of scrolling through them to find out whether my street was on the list, so this script does the scrolling for me. It checks the advisory feed every hour, has Claude read the images, and pings my phone through ntfy only when my feeder, substation or street is affected. The evening before a brownout it sends one more reminder so I remember to charge my stuff.

It was built for my own area. Change the config for yours.

## What you need

- python3 (standard library only)
- the `claude` CLI, installed and logged in (it runs `claude -p --model haiku` to read the images)
- tesseract (optional, but recommended; a quick OCR pass skips Claude when a post clearly has nothing to do with you)
- an ntfy topic and the ntfy app on your phone
- systemd, if you want it on a timer

## Config

Copy `config.example.json` to `config.json` next to the script (or point `BROWNOUT_CONFIG` at it somewhere else).

- `utility` is the power company's name, used in the prompt and alerts
- `feed` is the WordPress REST URL for its advisory posts (`/wp-json/wp/v2/posts?categories=<id>&_fields=id,date,title,link,content`)
- `feeders` are feeder ids as the utility writes them, like `1-7`
- `substations` are substation names; an alert fires if the whole substation goes down
- `streets` are street or area words to look for in the affected areas text
- `nearby` are words that only get logged, not alerted, so you can see near misses

Copy `brownout.env.example` to `brownout.env` and set `NTFY_TOPIC`. Set `NTFY_URL` if you self-host (it defaults to ntfy.sh). `BROWNOUT_HOME` (default `~/brownout`) is where `state.json` lives. With no topic set, alerts just go to the log.

## Running it

    python3 brownout.py               # check new posts, send alerts and reminders
    python3 brownout.py --dry         # same, but log alerts instead of sending
    python3 brownout.py --test-latest # parse the 3 newest posts and print what it found

To run it hourly, put the repo at `~/brownout` on the machine (along with your `config.json` and `brownout.env`), then:

    cp brownout.service brownout.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now brownout.timer

`deploy.sh` does the same thing over ssh: `DEPLOY_HOST=myserver ./deploy.sh`. It only ships committed files, so copy `config.json` and `brownout.env` over once by hand. If you want it running while you're logged out, `loginctl enable-linger` your user.
