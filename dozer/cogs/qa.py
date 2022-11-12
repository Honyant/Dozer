"""Provides commands that pull information from First Q&A Form."""
import discord

import aiohttp
import asyncio

from bs4 import BeautifulSoup

embed_color = discord.color.blue()

class QA(Cog):
    """QA commands"""
    def __init__(self, bot):
        super().__init__(bot)

    @qa.command()
    @bot_has_permissions(embed_links=True)
    async def qa(self, ctx, question: int):
        """
        Shows Answers from the FTC Q&A
        """
        async with aiohttp.ClientSession() as session:
          async with session.get('https://ftc-qa.firstinspires.org/onepage.html') as response:
              html_data = await response.text()

        answers =  BeautifulSoup(html_data, 'html.parser').get_text()

        start = answers.find('Q' + question + ' ')
        a = ""
        if(start > 0):

          finish = answers.find('answered',start) + 24
          a = answers[start:finish]

          #remove newlines
          a = a.replace("\n"," ")

          #remove multiple spaces
          a = " ".join(a.split())

          embed = discord.Embed(
                title=a[:a.find(" Q: ")],
                url="https://ftc-qa.firstinspires.org/qa/" + question,
                color=embed_color)

          embed.add_field(name="Question",
                          value=a[a.find(" Q: ")+1:a.find(" A: ")],
                          inline=False)
          embed.add_field(name="Answer",
                          value=a[a.find(" A: ")+1:a.find(" ( Asked by ")],
                          inline=False)

          embed.set_footer(
              text=a[a.find(" ( Asked by ")+1:])

          await ctx.send(embed=embed)

        else:
          a = "That question was not answered or does not exist."

          #add url
          a += "\nhttps://ftc-qa.firstinspires.org/qa/" + question
          await ctx.send(a)


    qa.example_usage = """
    `{prefix}qa 19` - show information on FTC Q&A #19
    """

def setup(bot):
    """Adds the QA cog to the bot."""
    bot.add_cog(QA(bot))
