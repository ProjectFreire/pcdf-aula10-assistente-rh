
"""
ETAPAS 5 E 9 — MCP Server do RH.

Etapa 5:
- Cinco ferramentas de leitura.
- Uma ferramenta de escrita idempotente.

Etapa 9:
- Validação de token.
- Validação das decisões permitidas.
- Proibição de autoaprovação (N8).
- Conferência da chefia responsável (N8).
- Auditoria de todas as tentativas de registro.
"""

import json
import os
from datetime import datetime
from hmac import compare_digest

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from dados_rh import (
    PASTA,
    SAIDAS,
    carregar_diarias,
    carregar_escala,
    carregar_servidores,
    normas,
)

load_dotenv(PASTA / ".env")

mcp = MCPServer("rh-mcp")

ARQUIVO_REGISTROS = SAIDAS / "registros.json"
ARQUIVO_AUDITORIA = SAIDAS / "auditoria.jsonl"

TOKEN_REGISTRO = os.getenv("TOKEN_REGISTRO", "")

DECISOES_VALIDAS = {
    "deferido",
    "indeferido",
    "indeferido_pela_chefia",
}

TIPOS_COM_CHEFIA = {
    "ferias",
    "diaria",
}


# ------------------------------------------------------------
# FUNÇÕES AUXILIARES
# ------------------------------------------------------------

def _ler_registros() -> dict:
    """Lê os registros gravados anteriormente."""

    if ARQUIVO_REGISTROS.exists():
        return json.loads(
            ARQUIVO_REGISTROS.read_text(
                encoding="utf-8"
            )
        )

    return {}


def _gravar_registros(registros: dict) -> None:
    """Grava os registros no arquivo JSON."""

    SAIDAS.mkdir(
        parents=True,
        exist_ok=True,
    )

    ARQUIVO_REGISTROS.write_text(
        json.dumps(
            registros,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------
# ETAPA 5.1 — FERRAMENTAS DE LEITURA
# ------------------------------------------------------------

@mcp.tool()
def consultar_servidor(matricula: str) -> dict:
    """Consulta o cadastro de um servidor pela matrícula.

    Retorna informações funcionais, sem divulgar salário.

    Args:
        matricula: Matrícula do servidor consultado.
    """

    servidores = carregar_servidores()
    servidor = servidores.get(matricula)

    if servidor is None:
        return {
            "erro": "matrícula não encontrada"
        }

    return {
        "matricula": matricula,
        "nome": servidor["nome"],
        "cargo": servidor["cargo"],
        "equipe": servidor["equipe"],
        "saldo_ferias_dias": servidor[
            "saldo_ferias_dias"
        ],
        "abonos_restantes": servidor[
            "abonos_restantes"
        ],
        "chefia": servidor["chefia"],
    }


@mcp.tool()
def consultar_escala(
    equipe: str,
    inicio: str,
    fim: str,
) -> dict:
    """Consulta afastamentos de uma equipe no período.

    Args:
        equipe: Nome da equipe.
        inicio: Data inicial no formato AAAA-MM-DD.
        fim: Data final no formato AAAA-MM-DD.
    """

    servidores = carregar_servidores()
    escala = carregar_escala()

    tamanho_equipe = sum(
        1
        for servidor in servidores.values()
        if servidor["equipe"] == equipe
    )

    limite_afastados = max(
        1,
        int(tamanho_equipe * 0.30),
    )

    afastamentos = [
        afastamento
        for afastamento in escala
        if (
            afastamento["equipe"] == equipe
            and afastamento["inicio"] <= fim
            and inicio <= afastamento["fim"]
        )
    ]

    return {
        "equipe": equipe,
        "tamanho_equipe": tamanho_equipe,
        "limite_afastados": limite_afastados,
        "afastamentos": afastamentos,
    }


@mcp.tool()
def consultar_normas(tema: str) -> dict:
    """Consulta normas relacionadas ao tema informado.

    Args:
        tema: Assunto das normas de RH.
    """

    return {
        "tema": tema,
        "regras": normas(tema),
    }


@mcp.tool()
def tabela_diarias(tipo_destino: str) -> dict:
    """Consulta o valor e a moeda das diárias.

    Args:
        tipo_destino: Capital, interior ou exterior.
    """

    diarias = carregar_diarias()

    destino = tipo_destino.strip().lower()

    if destino not in diarias:
        return {
            "erro": "tipo de destino não encontrado"
        }

    return {
        "tipo_destino": destino,
        "moeda": diarias[destino]["moeda"],
        "valor_diaria": diarias[destino][
            "valor_diaria"
        ],
    }


@mcp.tool()
def consultar_remuneracao(matricula: str) -> dict:
    """[RESTRITA] Consulta o salário-base do servidor.

    Uso destinado ao agente Financeiro.

    Args:
        matricula: Matrícula do servidor consultado.
    """

    servidores = carregar_servidores()
    servidor = servidores.get(matricula)

    if servidor is None:
        return {
            "erro": "matrícula não encontrada"
        }

    return {
        "matricula": matricula,
        "salario_base": servidor["salario_base"],
    }


# ------------------------------------------------------------
# ETAPA 9.3 — AUDITORIA
# ------------------------------------------------------------

def _auditar(
    protocolo: str,
    decisao_acesso: str,
    motivo: str,
    parametros: dict,
) -> None:
    """
    Registra uma tentativa de escrita no arquivo JSONL.

    Cada linha representa uma tentativa de registro.

    ALLOW = operação autorizada.
    DENY = operação negada.

    O token e o despacho não são gravados na auditoria.
    """

    SAIDAS.mkdir(
        parents=True,
        exist_ok=True,
    )

    parametros_seguros = {
        chave: valor
        for chave, valor in parametros.items()
        if chave not in ("token", "despacho")
    }

    evento = {
        "quando": datetime.now().isoformat(),
        "thread_id": parametros_seguros.get(
            "thread_id", ""
        ),
        "quem": parametros_seguros.get(
            "aprovador", ""
        ),
        "tool": "registrar_decisao",
        "protocolo": protocolo,
        "decisao_acesso": decisao_acesso,
        "motivo": motivo,
        "parametros": parametros_seguros,
    }

    with ARQUIVO_AUDITORIA.open(
        "a",
        encoding="utf-8",
    ) as arquivo:
        arquivo.write(
            json.dumps(
                evento,
                ensure_ascii=False,
            )
            + "\n"
        )


# ------------------------------------------------------------
# ETAPAS 5.2 E 9.2 — REGISTRO SEGURO
# ------------------------------------------------------------

@mcp.tool()
def registrar_decisao(
    protocolo: str,
    matricula: str,
    tipo: str,
    inicio: str,
    fim: str,
    decisao: str,
    aprovador: str,
    despacho: str,
    token: str,
    thread_id: str = "",
) -> dict:
    """[ESCRITA — requer autorização] Registra decisão de RH.

    Args:
        protocolo: Identificador único do pedido.
        matricula: Matrícula do servidor solicitante.
        tipo: Tipo de pedido.
        inicio: Data inicial do período.
        fim: Data final do período.
        decisao: Deferido, indeferido ou indeferido_pela_chefia.
        aprovador: Identificação de quem aprovou.
        despacho: Fundamentação da decisão.
        token: Token de autorização.
        thread_id: Identificador da execução.
    """

    parametros = {
        "matricula": matricula,
        "tipo": tipo,
        "inicio": inicio,
        "fim": fim,
        "decisao": decisao,
        "aprovador": aprovador,
        "thread_id": thread_id,
    }

    def negar(motivo: str) -> dict:
        """Audita a negativa e impede a gravação."""

        _auditar(
            protocolo,
            "DENY",
            motivo,
            parametros,
        )

        return {
            "sucesso": False,
            "erro": motivo,
        }

    # --------------------------------------------------------
    # 1. TOKEN DE AUTORIZAÇÃO
    # --------------------------------------------------------

    if (
        not TOKEN_REGISTRO
        or not compare_digest(
            str(token),
            TOKEN_REGISTRO,
        )
    ):
        return negar("token inválido")

    # --------------------------------------------------------
    # 2. ENUM FECHADO DE DECISÕES
    # --------------------------------------------------------

    if decisao not in DECISOES_VALIDAS:
        return negar("decisão inválida")

    # --------------------------------------------------------
    # 3. CONFERÊNCIA DO SERVIDOR
    # --------------------------------------------------------

    servidores = carregar_servidores()
    servidor = servidores.get(str(matricula))

    if servidor is None:
        return negar("matrícula não encontrada")

    # --------------------------------------------------------
    # 4. PROIBIÇÃO DE AUTOAPROVAÇÃO — N8
    # --------------------------------------------------------

    if str(aprovador) == str(matricula):
        return negar("autoaprovação (N8)")

    # --------------------------------------------------------
    # 5. CONFERÊNCIA DA CHEFIA — N8
    # --------------------------------------------------------

    # Pedidos de férias e diárias deferidos dependem
    # de aprovação da chefia responsável.
    #
    # Também se confere a chefia quando a decisão é
    # indeferido_pela_chefia.

    exige_chefia = (
        (
            tipo in TIPOS_COM_CHEFIA
            and decisao == "deferido"
        )
        or decisao == "indeferido_pela_chefia"
    )

    if exige_chefia:
        chefia_correta = str(
            servidor.get("chefia", "")
        )

        if str(aprovador) != chefia_correta:
            return negar(
                "aprovador não é a chefia "
                f"{chefia_correta} (N8)"
            )

    # --------------------------------------------------------
    # 6. IDEMPOTÊNCIA
    # --------------------------------------------------------

    registros = _ler_registros()

    if protocolo in registros:
        _auditar(
            protocolo,
            "ALLOW",
            "duplicado",
            parametros,
        )

        return {
            "sucesso": True,
            "duplicado": True,
            "protocolo": protocolo,
        }

    # --------------------------------------------------------
    # 7. REGISTRO AUTORIZADO
    # --------------------------------------------------------

    registros[protocolo] = {
        **parametros,
        "despacho": despacho,
        "registrado_em": datetime.now().isoformat(),
    }

    _gravar_registros(registros)

    _auditar(
        protocolo,
        "ALLOW",
        "gravado",
        parametros,
    )

    return {
        "sucesso": True,
        "duplicado": False,
        "protocolo": protocolo,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
