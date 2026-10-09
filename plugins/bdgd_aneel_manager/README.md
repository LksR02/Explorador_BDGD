# Explorador BDGD 0.21.0

Plugin QGIS para facilitar o acesso, a compreensão e o uso das bases BDGD da ANEEL.
Copyright © 2026 Lucas Aragão. Licença: GNU GPL v3.0 (GPL-3.0-only); veja LICENSE.

## Instalação e primeiro uso

Instale o ZIP pelo gerenciador de complementos do QGIS e abra Explorador BDGD.
Em Windows de 64 bits, o plugin prepara PostgreSQL/PostGIS em segundo plano,
gera credenciais locais e cria a conexão no QGIS. Nas próximas aberturas,
reutiliza o banco e inicia o servidor automaticamente.

Primeiro uso: internet, aproximadamente 480 MiB de download e 3 GiB livres
para os componentes. As bases ocupam espaço adicional. Versão experimental,
validada com QGIS 3.44.6; metadados declaram QGIS 3.28 a 3.99.

O banco novo inicia sem bases importadas. O usuário escolhe distribuidora e
anos. A importação das geodatabases para PostgreSQL é uma etapa separada.
Arquivos da ANEEL utiliza um catálogo CSV de publicações, informado nas
opções avançadas, com os campos id, type, title, tags e modified.

## Recursos

- Nomes acessíveis e descrições para entidades e campos, preservando códigos oficiais.
- Organização e carregamento de camadas por distribuidora, ano e tema.
- Download e exploração de arquivos a partir do catálogo de publicações.
- Análises descritivas, amostragem, indicadores, gráficos e exportações.
- Preservação das bases do banco local administrado pelo plugin.

Documentação e contribuições: https://github.com/LksR02/Explorador_BDGD
Os dados da ANEEL e os componentes de terceiros mantêm suas próprias licenças.
