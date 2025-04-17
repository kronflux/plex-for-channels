# Plex for Channels

## About

**Plex for Channels** is a utility for generating dynamic `.m3u` playlists and `epg.xml` files from Plex's linear TV feed, suitable for IPTV clients such as Jellyfin, Channels DVR, and others.

This project is a fork of the original [jgomez177/plex-for-channels](https://github.com/jgomez177/plex-for-channels), with significant improvements in HLS proxying, token handling, dynamic routing, logo caching, and EPG integration.

## Running

The recommended way to run is using the published Docker container:

```bash
docker run -d --restart unless-stopped --network=host \
    -e PORT=[your_port_number_here] \
    --name plex-for-channels \
    ghcr.io/kronflux/plex-for-channels
```

Alternatively:

```bash
docker run -d --restart unless-stopped \
    -p [your_port_number_here]:7777 \
    --name plex-for-channels \
    ghcr.io/kronflux/plex-for-channels
```

Once running, access the status page to retrieve the playlist and EPG URLs:

```
http://127.0.0.1:[your_port_number_here]
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT`   | Port the server listens on. Override if necessary to avoid conflicts. | 7777 |

## URL Parameters

These parameters can be added to the `/playlist.m3u` or `/epg.xml` request URLs.

| Parameter  | Description |
|------------|-------------|
| `regions`  | Comma-separated list of geo regions to include in the playlist. Example: `regions=local,nyc`<br>Defaults to `local`. |
| `gracenote`| Use `include` to filter to streams that provide Gracenote EPG metadata, or `exclude` to filter those out. |

---

For updates, see the [Releases](https://github.com/kronflux/plex-for-channels/releases) page.
