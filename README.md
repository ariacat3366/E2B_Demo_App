今から、ハッカソン向けのデモアプリを開発します。
要件は以下の通りです。
```
You can compete in teams of 1-4
​You can only choose one of the tracks: Online or offline.
​To qualify for winners, you need to submit a functioning code, a demo shorter than 2 minutes, and need to be using E2B sandbox, and at least one MCP from the Docker Hub
​Judges evaluate technical quality, innovation factor, and overall impression of your solution. To ensure fair evaluation, judges are developers, founders, and technical experts across companies.
​⏭ Submit online solution until 22. 11. 9:00AM (morning) PST
​⏭ Submit offline solution until 22. 11. 17:30 (evening) PST
​🚨 Submissions before start or after end of the hackathon don't count. You can only submit one project, and only choose one track (online, or offline).
```
これを踏まえて、以下のアプリケーションを開発します。
サービス概要：実装差分の可視化を行う、開発補助AIエージェント
機能：
GithubのmainブランチへのPRが作成されたタイミング or pushされたタイミングで（サービスの仕組み的に良い方を採用したいので提案してください）、
（おそらくmain）ブランチと新しいブランチの比較を行い、変更箇所をE2B上で実行し、差分を解説。
実行時、変更箇所が可視化できるもの（Frontで可視化できるものやterminalで実行結果が出るもの）である場合、変更前、変更後の実装をE2B上で表示、もしくは実行し、変更箇所をピックアップした上でスクリーンショットを行い、差分を解説する。

appendex 
- 時間があれば以下も実装したい
  - UI上で変更箇所を自然言語による指示
  - 変更箇所の実行結果を可視化（スクリーンショットなどによる差分表示）
  - branchを作成してPR作成

上記の実装を行っていきます。
まず、開発の方針を明確にし、順序立てて実装を行なっていくための指針を作成してください。
その後、その指針に沿って開発を進めていくので、適宜具体的な開発方法をヒアリングさせてください。