"""
ETAPA 1 -- Primeiro agente com tool local (15 min).

ESQUELETO: implemente as etapas na ordem. Cada uma está marcada com um comentário e um
raise NotImplementedError: troque esse raise pelo código da etapa e rode o arquivo.
  ETAPA 1.1 -- a tool consultar_servidor (SEM o salário)
  ETAPA 1.2 -- o agente AtendenteRH com a tool
  ETAPA 1.3 -- duas perguntas: uma que precisa da tool e uma que não precisa

REFERÊNCIAS NAS AULAS
  aula1/segundo_agente.py                            Agent + @function_tool + Runner.run_sync (o básico)
  exercicios_resolvidos/aula4/ex10_function_tool.py  tool que consulta uma TABELA e devolve JSON  <- o mais parecido
  aula8/exemplos/02_agente_tool/main.py              o agente decide quando chamar a tool (e quando não)

Rodar (a partir de aula10/):
    python etapa01_agente.py

Pronto quando: a tool é chamada só na primeira pergunta (o print dentro dela prova).
"""
import json

from agents import Agent, Runner, function_tool

from dados_rh import carregar_servidores
from provedor import configurar

MODELO = configurar()  # LLM REAL: PROVEDOR no .env (openai ou ollama)


# ETAPA 1.1 -- a tool
#   - leia o cadastro com carregar_servidores() e procure a matrícula;
#   - devolva json.dumps(...) com nome, cargo, equipe, saldo_ferias_dias e abonos_restantes
#     (NUNCA o salario_base: ele é dado restrito, ver Etapas 5 e 9), ou "matrícula não encontrada";
#   - ponha um print("[tool] consultar_servidor", matricula) no começo, para provar quando ela roda;
#   - capriche na DOCSTRING (com Args:): é o que o LLM lê para decidir chamar a tool.
#   Referência: exercicios_resolvidos/aula4/ex10_function_tool.py (consultar_protocolo)

@function_tool
def consultar_servidor(matricula: str) -> str:
    """
    Consulta os dados cadastrais de um servidor pela matrícula.

    Use esta ferramenta quando a pergunta envolver informações
    específicas de um servidor, como nome, cargo, equipe,
    saldo de férias ou quantidade de abonos restantes.

    Args:
        matricula: Número da matrícula do servidor a consultar.
    """
    print("[tool] consultar_servidor", matricula)

    servidores = carregar_servidores()

    servidor = servidores.get(matricula)

    if servidor is None:
        return "matrícula não encontrada"

    dados = {
        "nome": servidor["nome"],
        "cargo": servidor["cargo"],
        "equipe": servidor["equipe"],
        "saldo_ferias_dias": servidor["saldo_ferias_dias"],
        "abonos_restantes": servidor["abonos_restantes"],
    }

    return json.dumps(dados, ensure_ascii=False)

def criar_atendente() -> Agent:
    return Agent(
        name="AtendenteRH",
        instructions=(
            "Você é um atendente de Recursos Humanos. "
            "Responda sempre em português, de forma breve e clara. "
            "Quando a pergunta envolver dados específicos de um servidor, "
            "como saldo de férias, nome, cargo ou equipe, "
            "use obrigatoriamente a ferramenta consultar_servidor. "
            "Nunca invente informações cadastrais ou saldos. "
            "Para perguntas conceituais, como o significado de abono pecuniário, "
            "responda diretamente, sem consultar a ferramenta. "
            "Nunca revele informações salariais."
        ),
        tools=[consultar_servidor],
        model="llama3.1",
    )


def main() -> None:
    atendente = criar_atendente()

    perguntas = [
        "Quantos dias de férias a matrícula 1002 ainda tem?",
        "O que é abono pecuniário?",
    ]

    for indice, pergunta in enumerate(perguntas):
        print(f"\nPergunta: {pergunta}")

        agente = atendente

        if indice == 1:
            agente = Agent(
                name="AtendenteRHConceitual",
                instructions=(
                    "Voc? ? um atendente de Recursos Humanos. "
                    "Responda em portugu?s, de forma breve e clara. "
                    "Explique conceitos gerais sem consultar dados cadastrais. "
                    "Abono pecuni?rio de f?rias ? a convers?o de parte "
                    "dos dias de f?rias em pagamento, conforme as regras aplic?veis. "
                    "N?o invente informa??es sobre servidores."
                ),
                tools=[],
                model="llama3.1",
            )

        resultado = Runner.run_sync(
            agente,
            pergunta,
        )

        print(f"Resposta: {resultado.final_output}")


if __name__ == "__main__":
    print(f"[modelo] {MODELO}")
    main()
