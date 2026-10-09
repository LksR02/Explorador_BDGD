"""Human-readable BDGD entity and field catalog.

Physical table and field names are deliberately preserved.  This module only
controls presentation in the plugin and in layers added to a QGIS project.
"""
from __future__ import annotations

import re


THEMES = {
    "overview": "01 · Visão geral",
    "network": "02 · Rede elétrica",
    "equipment": "03 · Equipamentos",
    "customers": "04 · Consumidores e conexões",
    "generation": "05 · Geração",
    "indicators": "06 · Indicadores e dados comerciais",
    "auxiliary": "90 · Tabelas auxiliares",
}


def _entity(name, description, theme, voltage=""):
    return {"name": name, "description": description, "theme": theme, "voltage": voltage}


# Names follow the terminology used by the BDGD manual.  The catalog is
# intentionally useful even when a distributor publishes only a subset.
ENTITIES = {
    "SUB": _entity("Subestações", "Instalações destinadas à transformação, manobra e controle da rede.", "overview"),
    "CTAT": _entity("Circuitos de alta tensão", "Circuitos ou alimentadores da rede de alta tensão.", "network", "Alta tensão"),
    "CTMT": _entity("Alimentadores de média tensão", "Circuitos que distribuem energia a partir das subestações.", "overview", "Média tensão"),
    "SSDAT": _entity("Trechos da rede de alta tensão", "Segmentos lineares da rede de alta tensão.", "network", "Alta tensão"),
    "SSDMT": _entity("Trechos da rede de média tensão", "Segmentos lineares dos alimentadores de média tensão.", "network", "Média tensão"),
    "SSDBT": _entity("Trechos da rede de baixa tensão", "Segmentos lineares da rede de baixa tensão.", "network", "Baixa tensão"),
    "UNTRAT": _entity("Transformadores de alta tensão", "Unidades transformadoras associadas à rede de alta tensão.", "equipment", "Alta tensão"),
    "UNTRMT": _entity("Transformadores de distribuição", "Unidades transformadoras associadas à rede de média tensão.", "overview", "Média tensão"),
    "UNTRD": _entity("Transformadores de distribuição", "Unidades transformadoras de distribuição em estruturas históricas da BDGD.", "overview"),
    "UNSEAT": _entity("Chaves de alta tensão", "Unidades de seccionamento e manobra da rede.", "equipment", "Alta tensão"),
    "UNSEMT": _entity("Chaves de média tensão", "Unidades de seccionamento e manobra da rede.", "equipment", "Média tensão"),
    "UNSEBT": _entity("Chaves de baixa tensão", "Unidades de seccionamento e manobra da rede.", "equipment", "Baixa tensão"),
    "UNREAT": _entity("Reguladores de alta tensão", "Unidades reguladoras de tensão.", "equipment", "Alta tensão"),
    "UNREMT": _entity("Reguladores de média tensão", "Unidades reguladoras de tensão.", "equipment", "Média tensão"),
    "UNCRAT": _entity("Compensadores reativos de alta tensão", "Unidades de compensação de potência reativa.", "equipment", "Alta tensão"),
    "UNCRMT": _entity("Compensadores reativos de média tensão", "Unidades de compensação de potência reativa.", "equipment", "Média tensão"),
    "UNCRBT": _entity("Compensadores reativos de baixa tensão", "Unidades de compensação de potência reativa.", "equipment", "Baixa tensão"),
    "UCAT": _entity("Unidades consumidoras de alta tensão", "Instalações consumidoras atendidas em alta tensão.", "customers", "Alta tensão"),
    "UCMT": _entity("Unidades consumidoras de média tensão", "Instalações consumidoras atendidas em média tensão.", "customers", "Média tensão"),
    "UCBT": _entity("Unidades consumidoras de baixa tensão", "Instalações consumidoras atendidas em baixa tensão.", "customers", "Baixa tensão"),
    "UGAT": _entity("Unidades geradoras de alta tensão", "Unidades geradoras conectadas em alta tensão.", "generation", "Alta tensão"),
    "UGMT": _entity("Unidades geradoras de média tensão", "Unidades geradoras conectadas em média tensão.", "generation", "Média tensão"),
    "UGBT": _entity("Unidades geradoras de baixa tensão", "Unidades geradoras conectadas em baixa tensão.", "generation", "Baixa tensão"),
    "RAMLIG": _entity("Ramais de ligação", "Ligações entre a rede e unidades atendidas.", "customers"),
    "PIP": _entity("Pontos de iluminação pública", "Pontos de iluminação pública associados a pontos notáveis da rede.", "customers"),
    "PONNOT": _entity("Pontos notáveis", "Pontos de referência ou conexão relevantes para a rede.", "network"),
    "BAR": _entity("Barramentos", "Barramentos representados na rede elétrica.", "network"),
    "CONJ": _entity("Conjuntos elétricos", "Conjuntos de unidades consumidoras usados na organização e avaliação da distribuição.", "indicators"),
    "ARAT": _entity("Área de atuação", "Polígono da área de atuação da distribuidora.", "overview"),
    "BASE": _entity("Informações da base", "Metadados da BDGD enviada: distribuidora, data de referência e versão.", "auxiliary"),
    "BAY": _entity("Bays de subestação", "Módulos de manobra e conexão associados às subestações.", "equipment"),
    "BE": _entity("Balanço de energia", "Balanço energético declarado pela distribuidora.", "indicators"),
    "CRVCRG": _entity("Curvas de carga típicas", "Tipologias de curva de carga usadas para caracterizar o consumo (TIP_CC).", "indicators"),
    "EP": _entity("Energia passante", "Energia que flui entre segmentos e níveis de tensão da rede.", "indicators"),
    "EQCR": _entity("Equipamentos compensadores de reativo", "Cadastro técnico dos equipamentos instalados nas unidades compensadoras.", "equipment"),
    "EQME": _entity("Equipamentos de medição", "Cadastro técnico dos medidores.", "equipment"),
    "EQRE": _entity("Equipamentos reguladores", "Cadastro técnico dos equipamentos reguladores de tensão.", "equipment"),
    "EQSE": _entity("Equipamentos seccionadores", "Cadastro técnico de chaves e demais equipamentos de seccionamento.", "equipment"),
    "EQSIAT": _entity("Equipamentos do sistema de aterramento", "Cadastro técnico de equipamentos de aterramento (estrutura recente da BDGD).", "equipment"),
    "EQTRAT": _entity("Equipamentos transformadores de alta tensão", "Cadastro técnico dos transformadores de alta tensão.", "equipment", "Alta tensão"),
    "EQTRMT": _entity("Equipamentos transformadores de média tensão", "Cadastro técnico dos transformadores de média tensão.", "equipment", "Média tensão"),
    "EQTRD": _entity("Equipamentos transformadores de distribuição", "Cadastro técnico de transformadores de distribuição em estruturas históricas da BDGD.", "equipment"),
    "EQTRM": _entity("Equipamentos transformadores de medida", "Cadastro técnico de transformadores de corrente e de potencial.", "equipment"),
    "EQTRS": _entity("Equipamentos transformadores de subestação", "Cadastro técnico de transformadores de subestação em estruturas históricas da BDGD.", "equipment"),
    "EQTRSX": _entity("Equipamentos transformadores de subestação (complementar)", "Tabela histórica complementar de transformadores de subestação; confira o dicionário da BDGD do ano.", "equipment"),
    "UNTRS": _entity("Transformadores de subestação", "Unidades transformadoras de subestação em estruturas históricas da BDGD.", "equipment"),
    "INDGER": _entity("Indicadores gerenciais", "Indicadores gerenciais declarados pela distribuidora.", "indicators"),
    "PNT": _entity("Perdas não técnicas", "Perdas não técnicas declaradas por segmento.", "indicators"),
    "PT": _entity("Perdas técnicas", "Perdas técnicas declaradas por segmento.", "indicators"),
    "SEGCON": _entity("Segmentos condutores", "Tipos de condutores e seus parâmetros elétricos.", "network"),
}


FIELD_ALIASES = {
    "COD_ID": "Identificador do registro",
    "DIST": "Distribuidora — código ANEEL",
    "DESCR": "Descrição",
    "NOM": "Nome",
    "MUN": "Município — código IBGE",
    "SUB": "Subestação",
    "CONJ": "Conjunto elétrico",
    "CTMT": "Alimentador de média tensão",
    "CTAT": "Circuito de alta tensão",
    "UNI_TR_MT": "Transformador associado",
    "UNI_TR_AT": "Transformador de alta tensão associado",
    "PN_CON": "Ponto de conexão",
    "PAC": "Ponto de acoplamento comum",
    "GRU_TAR": "Grupo tarifário",
    "CLAS_SUB": "Classe e subclasse de consumo",
    "SIT_ATIV": "Situação da unidade",
    "DAT_CON": "Data de conexão",
    "CAR_INST": "Carga instalada (kW)",
    "POT_INST": "Potência instalada (kW)",
    "FAS_CON": "Fases de conexão",
    "ARE_LOC": "Área de localização",
    "TIP_CC": "Tipologia da curva de carga",
    "TEN_FORN": "Tensão de fornecimento",
    "CEG": "Código da unidade geradora",
    "CEG_GD": "Código de geração distribuída informado",
    "DIC": "Duração de interrupções (horas)",
    "FIC": "Frequência de interrupções",
}
for _month in range(1, 13):
    FIELD_ALIASES[f"ENE_{_month:02d}"] = f"Energia — mês {_month:02d} (kWh)"


PROFILES = {
    "essential": {
        "title": "Mapa essencial",
        "description": "Subestações, alimentadores, transformadores e principais trechos da rede.",
    },
    "network": {
        "title": "Rede elétrica",
        "description": "Rede, equipamentos e visão geral, sem carregar consumidores individuais.",
    },
    "customers": {
        "title": "Consumidores e conexões",
        "description": "Unidades consumidoras, ramais e pontos de conexão.",
    },
    "generation": {
        "title": "Geração",
        "description": "Unidades geradoras em todos os níveis de tensão.",
    },
    "complete": {
        "title": "Base completa — avançado",
        "description": "Todas as entidades disponíveis; camadas são adicionadas inicialmente ocultas.",
    },
}


def entity_code(table_name: str) -> str:
    """Return the official entity code from common table naming variants."""
    name = str(table_name or "").split(".")[-1].strip().upper()
    name = re.sub(r"(?:_TAB|_TABLE)$", "", name)
    return name


def entity_info(table_name: str) -> dict:
    code = entity_code(table_name)
    known = ENTITIES.get(code)
    if known:
        return {"code": code, **known, "known": True}
    if code.startswith("UC"):
        theme = "customers"
    elif code.startswith("UG"):
        theme = "generation"
    elif code.startswith(("UN", "EQ")):
        theme = "equipment"
    elif code.startswith(("SSD", "SEG", "CT", "BAR")):
        theme = "network"
    else:
        theme = "auxiliary"
    readable = code.replace("_", " ").title() or "Entidade sem nome"
    return {
        "code": code,
        "name": readable,
        "description": "Entidade BDGD ainda sem descrição amigável no catálogo do plugin.",
        "theme": theme,
        "voltage": "",
        "known": False,
    }


def display_name(table_name: str) -> str:
    info = entity_info(table_name)
    return f'{info["name"]} ({info["code"]})'


def theme_title(table_name: str) -> str:
    return THEMES[entity_info(table_name)["theme"]]


def matches_profile(table_name: str, profile: str) -> bool:
    info = entity_info(table_name)
    if profile == "complete":
        return True
    if profile == "essential":
        return info["code"] in {"SUB", "CTAT", "CTMT", "SSDAT", "SSDMT", "SSDBT", "UNTRAT", "UNTRMT", "UNTRD"}
    if profile == "network":
        return info["theme"] in {"overview", "network", "equipment"}
    return info["theme"] == profile


def apply_layer_semantics(layer, table_name: str) -> None:
    """Apply presentation metadata without changing the data source schema."""
    info = entity_info(table_name)
    layer.setName(display_name(table_name))
    layer.setCustomProperty("bdgd/entity_code", info["code"])
    layer.setCustomProperty("bdgd/theme", info["theme"])
    layer.setCustomProperty("bdgd/source_table", str(table_name))
    if hasattr(layer, "setAbstract"):
        layer.setAbstract(
            f'{info["description"]}\nCódigo oficial BDGD: {info["code"]}. '
            "O nome físico da fonte foi preservado para rastreabilidade."
        )
    fields = layer.fields()
    for index in range(fields.count()):
        original = fields.at(index).name()
        alias = FIELD_ALIASES.get(original.upper())
        if alias:
            layer.setFieldAlias(index, alias)
