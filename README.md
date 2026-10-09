# Explorador BDGD

Plugin para QGIS desenvolvido por **Lucas Aragão** para facilitar o acesso,
a compreensão e o uso das Bases de Dados Geográficas da Distribuidora
(BDGD), publicadas pela ANEEL. O objetivo é democratizar o uso desses dados
por profissionais do setor elétrico, pesquisadores, estudantes e demais
interessados.

## Instalação no QGIS

1. Baixe [bdgd_aneel_manager_0.21.0.zip](bdgd_aneel_manager_0.21.0.zip).
2. No QGIS, abra **Complementos → Gerenciar e Instalar Complementos →
   Instalar a partir de ZIP** e selecione o arquivo baixado.
3. Ative **Explorador BDGD** e abra o plugin pelo botão na barra de ferramentas
   ou pelo menu **Banco de dados → BDGD ANEEL**.
4. Aguarde o preparo automático do banco no primeiro uso. Não é necessário
   instalar ou configurar PostgreSQL/PostGIS manualmente.

**Versão atual:** 0.21.0, experimental. O preparo automático do banco está
disponível para **Windows de 64 bits**. A versão foi validada com QGIS 3.44.6;
os metadados declaram QGIS 3.28 a 3.99. A compatibilidade com QGIS 4 ainda
não foi validada. As análises usam NumPy, GDAL e Matplotlib no Python do QGIS.

O primeiro uso precisa de internet, aproximadamente **480 MiB de download**
e pelo menos **3 GiB livres** para os componentes do banco. As bases BDGD
ocupam espaço adicional. O QGIS continua disponível durante a preparação.

## O que a ferramenta oferece

- Preparo e início automáticos de PostgreSQL/PostGIS, com conexão pronta no QGIS.
- Organização das bases por distribuidora e ano e reconhecimento das camadas.
- Nomes e descrições acessíveis para entidades e campos, preservando os códigos oficiais.
- Perfis de carregamento para rede, consumidores, geração e visão geral.
- Consulta e carregamento de camadas das bases já importadas no PostgreSQL.
- Download e exploração de arquivos geográficos publicados pela ANEEL,
  a partir de um catálogo CSV de publicações.
- Análises descritivas de consumo, amostragem configurável, indicadores,
  gráficos, filtros e exportação de resultados com metadados de execução.

## Primeiro uso dos dados

O plugin prepara a infraestrutura do banco; **uma instalação nova começa
sem bases importadas**. O usuário escolhe as distribuidoras e os anos que
pretende estudar. A versão 0.21.0 não importa automaticamente geodatabases
para o PostgreSQL.

Para usar a origem **Arquivos da ANEEL**, informe o catálogo CSV em suas
opções avançadas. O catálogo não acompanha este repositório. Ele deve
conter os campos `id`, `type`, `title`, `tags` e `modified`, com as
publicações do tipo `File Geodatabase`. O complemento usa os identificadores
das publicações para obter os arquivos no ArcGIS. Depois, selecione a
distribuidora e os anos e utilize as ações de download da interface.

Na origem PostgreSQL, as bases disponíveis são reconhecidas por schemas
no padrão `distribuidora_AAAA`. Selecione a distribuidora, os anos e as
camadas para adicioná-las ao mapa ou definir uma análise. Bases já presentes
na instalação portátil administrada pelo projeto podem ser reutilizadas.

O banco gerenciado fica em `%LOCALAPPDATA%/ExploradorBDGD/postgres`, fora da
pasta do plugin, atende em `127.0.0.1` e é reutilizado nas próximas aberturas.
Credenciais são geradas localmente; a conexão salva no QGIS não contém senha.
QGIS e GDAL utilizam o arquivo libpq de credenciais da conta Windows. Os
detalhes técnicos e o histórico estão no
[README do plugin](plugins/bdgd_aneel_manager/README.md).

## Desenvolvimento e validação

O código do complemento está em `plugins/bdgd_aneel_manager/`.
Para gerar o ZIP, execute `scripts/package_plugin.py` com Python.
Para executar os testes, utilize o Python distribuído com seu QGIS:

```powershell
& '<OSGeo4W>/bin/python-qgis.bat' scripts/test_plugin.py
```

A versão 0.21.0 passou por **85 testes automatizados** e **10 verificações
de integração** com um banco isolado, incluindo PostGIS, preservação de
registros, reinício, conflito de porta, acesso por QGIS/GDAL e preparo pela
interface. O teste de integração usa os ZIPs oficiais em
`logs/bootstrap_downloads/`; essas dependências e os bancos de teste não
acompanham o repositório.

## Contribuições e licença

Relate problemas e sugestões em
[Issues](https://github.com/LksR02/Explorador_BDGD/issues).
Contribuições que reduzam barreiras de uso, melhorem a documentação ou ampliem
a compatibilidade são bem-vindas.

Copyright © 2026 Lucas Aragão. O código deste projeto é distribuído sob
**GNU General Public License v3.0 (GPL-3.0-only)**, conforme [LICENSE](LICENSE).
Os dados da ANEEL e os componentes de terceiros mantêm seus próprios termos
de uso e licenças. Este projeto é independente e não representa uma
ferramenta oficial da ANEEL ou do QGIS.

