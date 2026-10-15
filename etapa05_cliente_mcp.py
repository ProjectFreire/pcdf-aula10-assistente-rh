
"""
ETAPA 5 — Cliente MCP do RH.

5.3 — Agente AtendenteRH utilizando MCPServerStdio.
5.4 — Chamada direta às ferramentas MCP, sem LLM.
"""

import asyncio
import json
import os
import sys

from agents import Agent, Runner
from agents.mcp import MCPServerStdio
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dados_rh import PASTA

load_dotenv(PASTA / ".env")

SERVER = str(PASTA / "etapa05_mcp_rh.py")


async def chamar_mcp_async(tool: str, argumentos: dict) -> dict:
    """Executa diretamente uma ferramenta do servidor MCP."""

    parametros = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
    )

    async with stdio_client(
        parametros,
        errlog=sys.stderr,
    ) as (leitura, escrita):

        async with ClientSession(leitura, escrita) as sessao:
            await sessao.initialize()

            resposta = await sessao.call_tool(
                tool,
                arguments=argumentos,
            )

            if resposta.is_error:
                mensagem = (
                    resposta.content[0].text
                    if resposta.content
                    else "Erro desconhecido na ferramenta MCP."
                )
                raise RuntimeError(mensagem)

            if not resposta.content:
                raise RuntimeError(
                    "A ferramenta MCP não retornou conteúdo."
                )

            resultado = json.loads(resposta.content[0].text)

            if not isinstance(resultado, dict):
                raise RuntimeError(
                    "A ferramenta MCP não retornou um dicionário."
                )

            return resultado


def chamar_mcp(tool: str, argumentos: dict) -> dict:
    """Versão síncrona para o fluxo linear e os nós do grafo."""
    return asyncio.run(chamar_mcp_async(tool, argumentos))


async def perguntar_ao_atendente(pergunta: str) -> str:
    """Executa o AtendenteRH conectado ao servidor MCP."""

    async with MCPServerStdio(
        params={
            "command": sys.executable,
            "args": [SERVER],
        },
        name="rh",
    ) as servidor:

        agente = Agent(
            name="AtendenteRH",
            instructions=(
                "Você é um atendente de Recursos Humanos. "
                "Responda às perguntas utilizando as ferramentas MCP "
                "quando necessário. "
                "Não invente informações sobre servidores, escalas ou normas. "
                "Não revele salários ou informações financeiras restritas. "
                "Não registre decisões sem autorização."
            ),
            mcp_servers=[servidor],
            model="llama3.1",
        )

        resultado = await Runner.run(agente, pergunta)

        return str(resultado.final_output)


if __name__ == "__main__":

    if "--agente" in sys.argv:
        from provedor import configurar

        print(f"[modelo] {configurar()}")

        pergunta = (
            "A equipe Cartório tem alguém de férias "
            "entre 12 e 26/07/2027?"
        )

        print(asyncio.run(perguntar_ao_atendente(pergunta)))

    else:
        print(
            chamar_mcp(
                "consultar_servidor",
                {"matricula": "1002"},
            )
        )

        print(
            chamar_mcp(
                "consultar_escala",
                {
                    "equipe": "Cartório",
                    "inicio": "2027-07-12",
                    "fim": "2027-07-26",
                },
            )
        )

        print(
            chamar_mcp(
                "consultar_normas",
                {"tema": "abono"},
            )
        )

        registro = {
            "protocolo": "teste001",
            "matricula": "1001",
            "tipo": "abono",
            "inicio": "2027-04-09",
            "fim": "2027-04-09",
            "decisao": "deferido",
            "aprovador": "sistema",
            "despacho": "Despacho de teste.",
            "token": os.getenv("TOKEN_REGISTRO", ""),
        }

        print(
            "1ª gravação:",
            chamar_mcp("registrar_decisao", registro),
        )

        print(
            "2ª gravação:",
            chamar_mcp("registrar_decisao", registro),
            "<- deve indicar duplicado",
        )

        print(
            json.dumps(
                {
                    "sem_token": chamar_mcp(
                        "registrar_decisao",
                        {**registro, "token": "errado"},
                    )
                },
                ensure_ascii=False,
            ),
            "<- a validação do token será feita na Etapa 9.2",
        )
