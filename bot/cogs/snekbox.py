import datetime
import logging
import random
import re
import textwrap

from discord import Colour, Embed
from discord.ext.commands import (
    Bot, CommandError, Context, NoPrivateMessage, command, guild_only
)

from bot.constants import Channels, ERROR_REPLIES, NEGATIVE_REPLIES, Roles, URLs
from bot.decorators import InChannelCheckFailure, in_channel
from bot.utils.messages import wait_for_deletion


log = logging.getLogger(__name__)

CODE_TEMPLATE = """
venv_file = "/snekbox/.venv/bin/activate_this.py"
exec(open(venv_file).read(), dict(__file__=venv_file))

try:
{CODE}
except:
    import traceback
    print(traceback.format_exc())
"""

ESCAPE_REGEX = re.compile("[`\u202E\u200B]{3,}")
FORMATTED_CODE_REGEX = re.compile(
    r"^\s*"                               # any leading whitespace from the beginning of the string
    r"(?P<delim>(?P<block>```)|``?)"      # code delimiter: 1-3 backticks; (?P=block) only matches if it's a block
    r"(?(block)(?:(?P<lang>[a-z]+)\n)?)"  # if we're in a block, match optional language (only letters plus newline)
    r"(?:[ \t]*\n)*"                      # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                      # extract all code inside the markup
    r"\s*"                                # any more whitespace before the end of the code markup
    r"(?P=delim)"                         # match the exact same delimiter from the start again
    r"\s*$",                              # any trailing whitespace until the end of the string
    re.DOTALL | re.IGNORECASE             # "." also matches newlines, case insensitive
)
RAW_CODE_REGEX = re.compile(
    r"^(?:[ \t]*\n)*"                     # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                      # extract all the rest as code
    r"\s*$",                              # any trailing whitespace until the end of the string
    re.DOTALL                             # "." also matches newlines
)

BYPASS_ROLES = (Roles.owner, Roles.admin, Roles.moderator, Roles.helpers)


class Snekbox:
    """
    Safe evaluation using Snekbox
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.jobs = {}

    async def snekbox_send(self, code):
        headers = {"Content-Type": "application/json"}
        data = {"code": code}
        async with self.bot.http_session.post(URLs.snekbox, headers=headers, json=data) as resp:
            result = await resp.json()
        return result

    @command(name='eval', aliases=('e',))
    @guild_only()
    @in_channel(Channels.bot, bypass_roles=BYPASS_ROLES)
    async def eval_command(self, ctx: Context, *, code: str = None):
        """
        Run some code. get the result back. We've done our best to make
        this safe, but do let us know if you manage to find an issue
        with it!

        This command supports multiple lines of code, including code
        wrapped inside a formatted code block.
        """

        if ctx.author.id in self.jobs:
            return await ctx.send(
                f"{ctx.author.mention} You've already got a job running "
                "- please wait for it to finish!"
            )

        if not code:  # None or empty string
            return await ctx.invoke(self.bot.get_command("help"), "eval")

        log.info(
            f"Received code from {ctx.author.name}#{ctx.author.discriminator} "
            f"for evaluation:\n{code}"
        )

        self.jobs[ctx.author.id] = datetime.datetime.now()

        # extract and clean the code from markdown
        match = FORMATTED_CODE_REGEX.fullmatch(code)
        if match:
            code, block, lang, delim = match.group("code", "block", "lang", "delim")
            code = textwrap.dedent(code)
            if block:
                info = (f"'{lang}' highlighted" if lang else "plain") + " code block"
            else:
                info = f"{delim}-enclosed inline code"
            log.trace(f"Extracted {info} for evaluation:\n{code}")
        else:
            code = textwrap.dedent(RAW_CODE_REGEX.fullmatch(code).group("code"))
            log.trace(
                "Eval message contains not or badly formatted code, "
                f"stripping whitespace only:\n{code}")

        try:
            stripped_lines = [ln.strip() for ln in code.split('\n')]
            if all([line.startswith('#') for line in stripped_lines]):
                return await ctx.send(
                    f"{ctx.author.mention} Your eval job has completed.\n\n```[No output]```"
                )

            code = textwrap.indent(code, "    ")
            code = CODE_TEMPLATE.replace("{CODE}", code)

            async with ctx.typing():
                result = await self.snekbox_send(code)
                output = result["output"].strip(" \n")
                paste_link = None

                if "<@" in output:
                    output = output.replace("<@", "<@\u200B")  # Zero-width space

                if "<!@" in output:
                    output = output.replace("<!@", "<!@\u200B")  # Zero-width space

                if ESCAPE_REGEX.findall(output):
                    output = "Code block escape attempt detected; will not output result"
                else:
                    # the original output, to send to a pasting service if needed
                    full_output = output
                    truncated = False
                    if output.count("\n") > 0:
                        lines = enumerate(output.split("\n"), start=1)
                        output = [f"{i:03d} | {line}" for i, line in lines]
                        output = "\n".join(output)

                    if output.count("\n") > 10:
                        output = "\n".join(output.split("\n")[:10])

                        if len(output) >= 1000:
                            output = f"{output[:1000]}\n... (truncated - too long, too many lines)"
                        else:
                            output = f"{output}\n... (truncated - too many lines)"
                        truncated = True

                    elif len(output) >= 1000:
                        output = f"{output[:1000]}\n... (truncated - too long)"
                        truncated = True

                    if truncated:
                        try:
                            response = await self.bot.http_session.post(
                                URLs.paste_service.format(key="documents"),
                                data=full_output
                            )
                            data = await response.json()
                            if "key" in data:
                                paste_link = URLs.paste_service.format(key=data["key"])
                        except Exception:
                            log.exception("Failed to upload full output to paste service!")

                if output.strip():
                    if paste_link:
                        msg = (
                            f"{ctx.author.mention} Your eval job has completed.\n\n"
                            f"```py\n{output}\n```\nFull output: {paste_link}"
                        )
                    else:
                        msg = (
                            f"{ctx.author.mention} Your eval job has completed.\n\n```"
                            f"py\n{output}\n```"
                        )

                    response = await ctx.send(msg)
                    self.bot.loop.create_task(
                        wait_for_deletion(response, user_ids=(ctx.author.id,), client=ctx.bot)
                    )

                else:
                    await ctx.send(
                        f"{ctx.author.mention} Your eval job has completed.\n\n```[No output]```"
                    )
        finally:
            del self.jobs[ctx.author.id]

    @eval_command.error
    async def eval_command_error(self, ctx: Context, error: CommandError):
        embed = Embed(colour=Colour.red())

        if isinstance(error, NoPrivateMessage):
            embed.title = random.choice(NEGATIVE_REPLIES)
            embed.description = "You're not allowed to use this command in private messages."
            await ctx.send(embed=embed)

        elif isinstance(error, InChannelCheckFailure):
            embed.title = random.choice(NEGATIVE_REPLIES)
            embed.description = str(error)
            await ctx.send(embed=embed)

        else:
            original_error = getattr(error, 'original', "no original error")
            log.error(f"Unhandled error in snekbox eval: {error} ({original_error})")
            embed.title = random.choice(ERROR_REPLIES)
            embed.description = "Some unhandled error occurred. Sorry for that!"
            await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(Snekbox(bot))
    log.info("Cog loaded: Snekbox")
