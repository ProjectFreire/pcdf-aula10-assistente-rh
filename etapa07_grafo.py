
"""ETAPAS 7 E 8 — Fluxo de RH em LangGraph com aprovação humana e SQLite."""

import json
import operator
import os
import re
import sys
from datetime import date
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import etapa06_agentes as agentes
from dados.casos import CASOS
from dados_rh import SAIDAS
from etapa02_extracao import extrair
from etapa03_guardrails import (
    LIMIAR,
    MENSAGENS,
    encontrar_dado_sensivel,
    nota_escopo,
)
from etapa04_ferramentas import (
    TempoEsgotado,
    gerar_protocolo,
    rodar_com_limites_sync,
)
from etapa05_cliente_mcp import chamar_mcp
from etapa06_regras import (
    precisa_chefia,
    resumo_periodo,
    violacoes_objetivas,
)
from etapa10_observabilidade import medido
from provedor import configurar


MODELO = configurar()
MAX_TENTATIVAS = 3
MAX_REJEICOES = 2
TEMPO_AGENTE = 35
GUARDRAIL_LIGADO = "--sem-guardrail" not in sys.argv


class Estado(TypedDict, total=False):
    thread_id: str
    texto: str
    bloqueado: bool
    motivo_bloqueio: str
    nota_escopo: float
    triagem: dict
    pedido: dict
    divergencia: bool
    servidor: dict
    periodo: dict
    escala: dict
    parecer_normas: str
    parecer_escala: str
    parecer_financeiro: str
    violacoes: Annotated[list[str], operator.add]
    decisao: str
    despacho: str
    tentativas: int
    erro_validacao: str
    rejeicoes: int
    feedback_chefia: str
    aprovado_chefia: bool
    aprovador: str
    protocolo: str
    resultado: str
    caminho: Annotated[list[str], operator.add]


def receber(estado: Estado) -> dict:
    return {
        "texto": estado["texto"].strip(),
        "tentativas": 0,
        "rejeicoes": 0,
        "violacoes": [],
        "erro_validacao": "",
        "feedback_chefia": "",
        "aprovador": "",
        "caminho": ["receber"],
    }


def recusar(estado: Estado) -> dict:
    return {
        "resultado": "recusado",
        "despacho": estado.get(
            "motivo_bloqueio", "Pedido recusado."
        ),
        "caminho": ["recusar"],
    }


def pedir_dados(estado: Estado) -> dict:
    if estado.get("divergencia"):
        faltam = (
            "as datas e o tipo do pedido "
            "(a leitura ficou ambígua)"
        )
    else:
        faltam = "a matrícula e as datas exatas (AAAA-MM-DD)"

    return {
        "resultado": "pediu_dados",
        "despacho": f"Para seguir, informe {faltam}.",
        "caminho": ["pedir_dados"],
    }


# ------------------------------------------------------------
# ETAPA 7.2 — Entrada e roteadores
# ------------------------------------------------------------

def proteger_entrada(estado: Estado) -> dict:
    if not GUARDRAIL_LIGADO:
        return {
            "bloqueado": False,
            "nota_escopo": 0.0,
            "caminho": ["proteger_entrada"],
        }

    texto = estado["texto"]
    sensivel = encontrar_dado_sensivel(texto)

    if sensivel:
        chave = str(sensivel).lower()

        if "cpf" in chave:
            mensagem = MENSAGENS.get(
                "cpf", "Não informe CPF no pedido."
            )
        elif "cid" in chave:
            mensagem = MENSAGENS.get(
                "cid", "Não informe CID no pedido."
            )
        else:
            mensagem = (
                "O pedido contém dados sensíveis. Remova-os."
            )

        return {
            "bloqueado": True,
            "motivo_bloqueio": mensagem,
            "nota_escopo": 0.0,
            "caminho": ["proteger_entrada"],
        }

    nota, motivo = nota_escopo(texto)
    bloqueado = nota >= LIMIAR

    return {
        "bloqueado": bloqueado,
        "motivo_bloqueio": (
            MENSAGENS.get("fora_do_escopo", str(motivo))
            if bloqueado
            else ""
        ),
        "nota_escopo": nota,
        "caminho": ["proteger_entrada"],
    }


def rotear_entrada(estado: Estado) -> str:
    return "bloqueado" if estado.get("bloqueado") else "ok"


def _preencher_campos_literais(
    texto: str, pedido: dict
) -> dict:
    """Recupera somente matrícula e datas explícitas."""
    pedido = dict(pedido)

    if not pedido.get("matricula"):
        m = re.search(
            r"\bmatr[ií]cula\s*(?:n[º°.]?\s*)?"
            r"[:#-]?\s*(\d{4})\b",
            texto,
            re.I,
        )
        if m:
            pedido["matricula"] = m.group(1)

    datas = []

    for m in re.finditer(
        r"\b\d{4}-\d{2}-\d{2}\b|"
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        texto,
    ):
        try:
            valor = m.group(0)

            if "/" in valor:
                dia, mes, ano = map(int, valor.split("/"))
                valor = date(ano, mes, dia).isoformat()
            else:
                valor = date.fromisoformat(valor).isoformat()

            datas.append(valor)

        except ValueError:
            continue

    if not pedido.get("data_inicio") and datas:
        pedido["data_inicio"] = datas[0]

    if not pedido.get("data_fim"):
        if (
            pedido.get("tipo") == "abono"
            and pedido.get("data_inicio")
        ):
            pedido["data_fim"] = pedido["data_inicio"]
        elif len(datas) > 1:
            pedido["data_fim"] = datas[1]

    return pedido


def no_extrair(estado: Estado) -> dict:
    pedido = extrair(
        estado["texto"]
    ).model_dump(mode="json")

    pedido = _preencher_campos_literais(
        estado["texto"], pedido
    )

    return {
        "pedido": pedido,
        "divergencia": False,
        "caminho": ["extrair"],
    }


def rotear_extracao(estado: Estado) -> str:
    pedido = estado.get("pedido") or {}

    if estado.get("divergencia") or not all(
        pedido.get(campo)
        for campo in (
            "matricula",
            "data_inicio",
            "data_fim",
        )
    ):
        return "incompleto"

    if pedido.get("tipo") not in (
        "ferias", "abono", "diaria"
    ):
        return "incompleto"

    try:
        inicio = date.fromisoformat(
            pedido["data_inicio"]
        )
        fim = date.fromisoformat(
            pedido["data_fim"]
        )

        if fim < inicio:
            return "incompleto"

    except (ValueError, TypeError):
        return "incompleto"

    return "ok"


def identificar_servidor(estado: Estado) -> dict:
    matricula = str(
        estado["pedido"]["matricula"]
    )

    resposta = chamar_mcp(
        "consultar_servidor",
        {"matricula": matricula},
    )

    if not isinstance(resposta, dict):
        resposta = {
            "erro": "Resposta inválida do MCP."
        }

    if not resposta.get("erro"):
        resposta = {
            **resposta,
            "matricula": matricula,
        }

    return {
        "servidor": resposta,
        "caminho": ["identificar_servidor"],
    }


def rotear_servidor(estado: Estado) -> str:
    servidor = estado.get("servidor") or {}

    return (
        "nao_encontrado"
        if servidor.get("erro")
        else "ok"
    )


def calcular_periodo(estado: Estado) -> dict:
    pedido = estado["pedido"]

    periodo = resumo_periodo(
        pedido["data_inicio"],
        pedido["data_fim"],
    )

    escala = chamar_mcp(
        "consultar_escala",
        {
            "equipe": estado["servidor"]["equipe"],
            "inicio": pedido["data_inicio"],
            "fim": pedido["data_fim"],
        },
    )

    if (
        not isinstance(escala, dict)
        or escala.get("erro")
    ):
        raise RuntimeError(
            f"Consulta da escala falhou: {escala}"
        )

    return {
        "periodo": periodo,
        "escala": escala,
        "caminho": ["calcular_periodo"],
    }


# ------------------------------------------------------------
# ETAPA 7.3 — Especialistas em paralelo
# ------------------------------------------------------------

def _fatos(estado: Estado) -> str:
    servidor = estado["servidor"]

    servidor_publico = {
        k: servidor.get(k)
        for k in (
            "matricula",
            "nome",
            "cargo",
            "equipe",
            "saldo_ferias_dias",
            "abonos_restantes",
        )
    }

    return json.dumps(
        {
            "pedido": estado["pedido"],
            "servidor": servidor_publico,
            "periodo": estado["periodo"],
            "escala": estado["escala"],
        },
        ensure_ascii=False,
        default=str,
    )


def _executar_especialista(
    fabrica, entrada: str, nome: str
) -> str:
    try:
        resultado = rodar_com_limites_sync(
            fabrica(),
            entrada,
            timeout_s=TEMPO_AGENTE,
        )

        texto = str(
            resultado.final_output or ""
        ).strip()

        if texto:
            return texto

        return (
            f"{nome}: parecer indisponível "
            "(resposta vazia)."
        )

    except TempoEsgotado:
        return (
            f"{nome}: parecer indisponível "
            f"(limite de {TEMPO_AGENTE}s)."
        )

    except Exception as erro:
        return (
            f"{nome}: parecer indisponível "
            f"({type(erro).__name__})."
        )


def no_normas(estado: Estado) -> dict:
    parecer = _executar_especialista(
        agentes.criar_normas,
        "Consulte as normas oficiais aplicáveis "
        "e explique as regras.\n"
        + _fatos(estado),
        "Normas",
    )

    return {
        "parecer_normas": parecer,
        "caminho": ["normas"],
    }


def no_escala(estado: Estado) -> dict:
    parecer = _executar_especialista(
        agentes.criar_escala,
        "Consulte a escala oficial e descreva "
        "conflitos sem inventar disponibilidade.\n"
        + _fatos(estado),
        "Escala",
    )

    return {
        "parecer_escala": parecer,
        "caminho": ["escala"],
    }


def no_financeiro(estado: Estado) -> dict:
    pedido = estado["pedido"]

    if pedido["tipo"] == "abono" or (
        pedido["tipo"] == "ferias"
        and not pedido.get("dias_vendidos", 0)
    ):
        parecer = (
            "Não se aplica: pedido sem "
            "cálculo financeiro."
        )

    else:
        entrada = (
            "Calcule apenas o valor financeiro "
            "pertinente, consultando as ferramentas. "
            "Não revele salário-base.\n"
            + _fatos(estado)
        )

        parecer = _executar_especialista(
            agentes.criar_financeiro,
            entrada,
            "Financeiro",
        )

    return {
        "parecer_financeiro": parecer,
        "caminho": ["financeiro"],
    }


def consolidar(estado: Estado) -> dict:
    violacoes = violacoes_objetivas(
        estado["pedido"],
        estado["servidor"],
        estado["periodo"],
        estado["escala"],
    )

    return {
        "violacoes": violacoes,
        "decisao": (
            "indeferido"
            if violacoes
            else "deferido"
        ),
        "caminho": ["consolidar"],
    }


# ------------------------------------------------------------
# ETAPA 7.4 — Redação e validação
# ------------------------------------------------------------

def _despacho_objetivo(estado: Estado) -> str:
    pedido = estado["pedido"]
    decisao = estado["decisao"]

    texto = (
        f"Pedido de {pedido['tipo']} "
        f"da matrícula {pedido['matricula']}, "
        f"período de {pedido['data_inicio']} "
        f"a {pedido['data_fim']}. "
        f"Decisão técnica: {decisao}. "
    )

    if estado.get("violacoes"):
        texto += (
            "Fundamentos: "
            + "; ".join(estado["violacoes"])
            + ". "
        )

    elif precisa_chefia(
        pedido,
        estado.get("violacoes", []),
    ):
        texto += (
            "A aprovação definitiva depende "
            "de manifestação da chefia. "
        )

    else:
        texto += (
            "Não foram identificadas "
            "violações objetivas. "
        )

    return texto.strip()


def redigir_despacho(estado: Estado) -> dict:
    entrada = json.dumps(
        {
            "fatos": _fatos(estado),
            "decisao": estado["decisao"],
            "violacoes": estado.get(
                "violacoes", []
            ),
            "parecer_normas": estado.get(
                "parecer_normas", ""
            ),
            "parecer_escala": estado.get(
                "parecer_escala", ""
            ),
            "parecer_financeiro": estado.get(
                "parecer_financeiro", ""
            ),
            "erro_validacao": estado.get(
                "erro_validacao", ""
            ),
            "feedback_chefia": estado.get(
                "feedback_chefia", ""
            ),
        },
        ensure_ascii=False,
        default=str,
    )

    pareceres = (
        estado.get("parecer_normas", ""),
        estado.get("parecer_escala", ""),
        estado.get("parecer_financeiro", ""),
    )

    todos_indisponiveis = all(
        "parecer indisponível" in p
        or "Não se aplica" in p
        for p in pareceres
    )

    if todos_indisponiveis:
        despacho = _despacho_objetivo(estado)

    else:
        despacho = _executar_especialista(
            agentes.criar_redator,
            "Redija despacho com matrícula, "
            "data inicial, decisão, fundamentos "
            "e pendência da chefia quando aplicável. "
            "Não invente dados.\n"
            + entrada,
            "Redator",
        )

        if "parecer indisponível" in despacho:
            despacho = _despacho_objetivo(estado)

    return {
        "despacho": despacho,
        "caminho": ["redigir_despacho"],
    }


def validar(estado: Estado) -> dict:
    pedido = estado["pedido"]
    despacho = estado.get("despacho", "")
    erros = []

    if str(pedido["matricula"]) not in despacho:
        erros.append("matrícula ausente")

    if pedido["data_inicio"] not in despacho:
        erros.append("data inicial ausente")

    decisao = estado["decisao"]

    if decisao == "indeferido":
        decisao_presente = bool(
            re.search(
                r"\bindeferid[oa]\b|"
                r"\bindefere-se\b|"
                r"\bindeferimento\b",
                despacho,
                re.I,
            )
        )
    else:
        decisao_presente = bool(
            re.search(
                r"\bdeferid[oa]\b|"
                r"\bdefere-se\b|"
                r"\bdeferimento\b",
                despacho,
                re.I,
            )
        )

    if not decisao_presente:
        erros.append("decisão ausente")

    if encontrar_dado_sensivel(despacho):
        erros.append("dado sensível na saída")

    return {
        "tentativas": (
            estado.get("tentativas", 0) + 1
        ),
        "erro_validacao": "; ".join(erros),
        "caminho": ["validar"],
    }


def rotear_validacao(estado: Estado) -> str:
    if (
        estado.get("erro_validacao")
        and estado.get("tentativas", 0)
        < MAX_TENTATIVAS
    ):
        return "redigir"

    if precisa_chefia(
        estado["pedido"],
        estado.get("violacoes", []),
    ):
        return "chefia"

    return "registrar"


# ------------------------------------------------------------
# ETAPA 8.2 — Aprovação humana
# ------------------------------------------------------------

def aprovacao_chefia(estado: Estado) -> dict:
    pedido = estado["pedido"]

    resposta = interrupt(
        {
            "pergunta": "Aprova o despacho?",
            "chefia": str(
                estado["servidor"].get("chefia", "")
            ),
            "resumo": {
                "servidor": estado["servidor"].get(
                    "nome", ""
                ),
                "matricula": str(
                    pedido["matricula"]
                ),
                "tipo": pedido["tipo"],
                "inicio": pedido["data_inicio"],
                "fim": pedido["data_fim"],
                "parecer_normas": estado.get(
                    "parecer_normas", ""
                ),
                "parecer_escala": estado.get(
                    "parecer_escala", ""
                ),
                "parecer_financeiro": estado.get(
                    "parecer_financeiro", ""
                ),
                "despacho": estado.get(
                    "despacho", ""
                ),
            },
        }
    )

    aprovador = str(
        resposta.get("aprovador", "")
    )

    if resposta.get("aprovado"):
        return {
            "aprovador": aprovador,
            "aprovado_chefia": True,
            "feedback_chefia": "",
            "caminho": ["aprovacao_chefia"],
        }

    rejeicoes = (
        estado.get("rejeicoes", 0) + 1
    )

    retorno = {
        "aprovador": aprovador,
        "aprovado_chefia": False,
        "rejeicoes": rejeicoes,
        "feedback_chefia": str(
            resposta.get("motivo", "")
        ),
        "tentativas": 0,
        "caminho": ["aprovacao_chefia"],
    }

    if rejeicoes >= MAX_REJEICOES:
        retorno["decisao"] = (
            "indeferido_pela_chefia"
        )

    return retorno


def rotear_chefia(estado: Estado) -> str:
    if (
        estado.get("rejeicoes", 0)
        >= MAX_REJEICOES
    ):
        return "registrar"

    if not estado.get(
        "aprovado_chefia", False
    ):
        return "redigir"

    return "registrar"


# ------------------------------------------------------------
# ETAPA 7.5 / 8.3 — Registro
# ------------------------------------------------------------

def registrar(estado: Estado) -> dict:
    pedido = estado["pedido"]
    despacho = estado.get("despacho", "")

    protocolo = gerar_protocolo(
        str(pedido["matricula"]),
        pedido["tipo"],
        pedido["data_inicio"],
        pedido["data_fim"],
    )

    if (
        estado.get("erro_validacao")
        or encontrar_dado_sensivel(despacho)
    ):
        return {
            "protocolo": protocolo,
            "resultado": "bloqueado_na_saida",
            "caminho": ["registrar"],
        }

    aprovador = (
        estado.get("aprovador")
        or "sistema"
    )

    decisao = (
        "indeferido_pela_chefia"
        if estado.get("rejeicoes", 0)
        >= MAX_REJEICOES
        else estado["decisao"]
    )

    dados = {
        "protocolo": protocolo,
        "matricula": str(
            pedido["matricula"]
        ),
        "tipo": pedido["tipo"],
        "inicio": pedido["data_inicio"],
        "fim": pedido["data_fim"],
        "decisao": decisao,
        "aprovador": aprovador,
        "despacho": despacho,
        "token": os.getenv(
            "TOKEN_REGISTRO", ""
        ),
        "thread_id": estado.get(
            "thread_id", ""
        ),
    }

    resposta = chamar_mcp(
        "registrar_decisao",
        dados,
    )

    if isinstance(resposta, dict):
        if resposta.get("duplicado") is True:
            resultado = "duplicado"
        elif resposta.get("sucesso") is True:
            resultado = "registrado"
        else:
            resultado = "negado_pelo_server"
    else:
        resultado = "negado_pelo_server"

    return {
        "protocolo": protocolo,
        "aprovador": aprovador,
        "resultado": resultado,
        "caminho": ["registrar"],
    }


# ------------------------------------------------------------
# ETAPA 8.3 — Construção do grafo
# ------------------------------------------------------------

def construir(checkpointer=None):
    grafo = StateGraph(Estado)

    nos = {
        "receber": receber,
        "proteger_entrada": proteger_entrada,
        "recusar": recusar,
        "extrair": no_extrair,
        "pedir_dados": pedir_dados,
        "identificar_servidor": identificar_servidor,
        "calcular_periodo": calcular_periodo,
        "normas": no_normas,
        "escala": no_escala,
        "financeiro": no_financeiro,
        "consolidar": consolidar,
        "redigir_despacho": redigir_despacho,
        "validar": validar,
        "aprovacao_chefia": aprovacao_chefia,
        "registrar": registrar,
    }

    for nome, funcao in nos.items():
        grafo.add_node(
            nome,
            medido(nome, funcao),
        )

    grafo.add_edge(
        START, "receber"
    )

    grafo.add_edge(
        "receber", "proteger_entrada"
    )

    grafo.add_conditional_edges(
        "proteger_entrada",
        rotear_entrada,
        {
            "bloqueado": "recusar",
            "ok": "extrair",
        },
    )

    grafo.add_edge("recusar", END)

    grafo.add_conditional_edges(
        "extrair",
        rotear_extracao,
        {
            "incompleto": "pedir_dados",
            "ok": "identificar_servidor",
        },
    )

    grafo.add_conditional_edges(
        "identificar_servidor",
        rotear_servidor,
        {
            "nao_encontrado": "pedir_dados",
            "ok": "calcular_periodo",
        },
    )

    grafo.add_edge("pedir_dados", END)

    for especialista in (
        "normas",
        "escala",
        "financeiro",
    ):
        grafo.add_edge(
            "calcular_periodo",
            especialista,
        )

    grafo.add_edge(
        ["normas", "escala", "financeiro"],
        "consolidar",
    )

    grafo.add_edge(
        "consolidar",
        "redigir_despacho",
    )

    grafo.add_edge(
        "redigir_despacho",
        "validar",
    )

    grafo.add_conditional_edges(
        "validar",
        rotear_validacao,
        {
            "redigir": "redigir_despacho",
            "chefia": "aprovacao_chefia",
            "registrar": "registrar",
        },
    )

    grafo.add_conditional_edges(
        "aprovacao_chefia",
        rotear_chefia,
        {
            "redigir": "redigir_despacho",
            "registrar": "registrar",
        },
    )

    grafo.add_edge("registrar", END)

    return grafo.compile(
        checkpointer=checkpointer
    )


# ------------------------------------------------------------
# ETAPA 8.4 — Persistência e retomada
# ------------------------------------------------------------

def config_de(thread_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id
        },
        "recursion_limit": 40,
    }


def mostrar(
    app, thread_id: str, saida: dict
) -> None:
    estado = app.get_state(
        config_de(thread_id)
    )

    dados = {
        **(estado.values or {}),
        **saida,
    }

    print(
        f"\n[{thread_id}] caminho: "
        f"{' -> '.join(dados.get('caminho', []))}"
    )

    if estado.next:
        pergunta = ""

        interrupcoes = saida.get(
            "__interrupt__", ()
        )

        if interrupcoes:
            valor = interrupcoes[0].value

            if isinstance(valor, dict):
                pergunta = valor.get(
                    "pergunta", ""
                )

        if not pergunta:
            if "aprovacao_chefia" in estado.next:
                pergunta = "Aprova o despacho?"
            else:
                pergunta = (
                    "Aguardando decisão humana"
                )

        print(
            f"[{thread_id}] PAUSADO em "
            f"{estado.next}: {pergunta}"
        )

        print(
            "   retome com: "
            "python etapa07_grafo.py "
            f"--retomar {thread_id} "
            'sim|"nao:motivo" <quem>'
        )

    else:
        print(
            f"[{thread_id}] resultado: "
            f"{dados.get('resultado')}  "
            f"protocolo: {dados.get('protocolo')}"
        )

        print(
            f"[{thread_id}] despacho: "
            f"{dados.get('despacho')}"
        )


def executar(
    app, thread_id: str, texto: str
) -> None:
    if app.checkpointer:
        app.checkpointer.delete_thread(
            thread_id
        )

    saida = app.invoke(
        {
            "thread_id": thread_id,
            "texto": texto,
        },
        config_de(thread_id),
    )

    mostrar(
        app, thread_id, saida
    )


def listar_pendentes(
    app, checkpointer
) -> None:
    threads = set()

    for cp in checkpointer.list(None):
        tid = cp.config.get(
            "configurable", {}
        ).get("thread_id")

        if tid:
            threads.add(str(tid))

    if not threads:
        print(
            "Nenhum processo encontrado."
        )

    for tid in sorted(
        threads, reverse=True
    ):
        proximo = app.get_state(
            config_de(tid)
        ).next

        if proximo:
            print(
                f"{tid}: PAUSADO em "
                f"{proximo}"
            )
        else:
            print(
                f"{tid}: concluído"
            )


def retomar(
    app,
    thread_id: str,
    resposta: str,
    quem: str,
) -> None:
    proximo = app.get_state(
        config_de(thread_id)
    ).next

    if not proximo:
        print(
            f"[{thread_id}] nada pausado"
        )
        return

    texto = resposta.strip()

    escolha, _, motivo = texto.partition(
        ":"
    )

    escolha = escolha.strip().lower()

    if "aprovacao_chefia" in proximo:
        if escolha not in (
            "sim", "nao", "não"
        ):
            print(
                'Responda "sim" ou "nao:motivo".'
            )
            return

        valor = {
            "aprovado": escolha == "sim",
            "aprovador": quem,
            "motivo": motivo.strip(),
        }

    elif "triagem_humana" in proximo:
        valor = {
            "segue": escolha == "sim",
            "orientacao": motivo.strip(),
            "quem": quem,
        }

    else:
        print(
            f"[{thread_id}] pausa desconhecida: "
            f"{proximo}"
        )
        return

    saida = app.invoke(
        Command(resume=valor),
        config_de(thread_id),
    )

    mostrar(
        app, thread_id, saida
    )


def main() -> None:
    args = [
        a for a in sys.argv[1:]
        if a != "--sem-guardrail"
    ]

    SAIDAS.mkdir(
        parents=True,
        exist_ok=True,
    )

    with SqliteSaver.from_conn_string(
        str(SAIDAS / "checkpoints.db")
    ) as checkpointer:

        app = construir(checkpointer)

        if (
            not args
            or args[0] == "--mermaid"
        ):
            print(
                app.get_graph().draw_mermaid()
            )

        elif args[0] == "--pendentes":
            listar_pendentes(
                app, checkpointer
            )

        elif args[0] == "--retomar":
            retomar(
                app,
                args[1],
                args[2],
                args[3] if len(args) > 3 else "?",
            )

        elif args[0] == "--todos":
            for nome, caso in CASOS.items():
                executar(
                    app,
                    nome,
                    caso["pedido"],
                )

        else:
            executar(
                app,
                args[0],
                CASOS[args[0]]["pedido"],
            )


if __name__ == "__main__":
    print(
        f"[modelo] {MODELO}  "
        "[guardrail de entrada "
        f"{'ligado' if GUARDRAIL_LIGADO else 'DESLIGADO'}]"
    )

    main()
