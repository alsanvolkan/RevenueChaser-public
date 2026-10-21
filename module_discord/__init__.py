"""Discord archive (#72): DiscordChatExporter -> ``.store/discord.duckdb``.

S1 is the store and the exporter only. Messages are untrusted third-party text: this
package keeps them verbatim and renders nothing; the board (S2) escapes them.
"""
