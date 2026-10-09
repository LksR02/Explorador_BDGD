# Validação do Explorador BDGD

A versão 0.21.0 foi verificada no Python do QGIS 3.44.6 com 85 testes
automatizados e 10 verificações de integração em um banco isolado.

O teste de integração cobre criação do banco com pacotes oficiais, ativação
do PostGIS, preservação de registros sintéticos, reabertura, reinício,
conflito de porta, leitura pelo QGIS/GDAL e abertura automática pela interface.

Os testes utilizam dados sintéticos e credenciais criadas localmente. Bancos,
credenciais e relatórios de análises locais não acompanham esta distribuição.
Para reproduzir os testes, utilize scripts/test_plugin.py e
scripts/test_managed_database_integration.py com o Python do QGIS.
O teste de integração exige os ZIPs oficiais previamente baixados em
logs/bootstrap_downloads/; veja as URLs e checksums em managed_database.py.

A validação é descritiva e funcional. O plugin não fornece um método de
previsão de demanda validado e não substitui a avaliação de qualidade dos
dados, cobertura histórica, chaves cadastrais ou hipóteses de amostragem.
