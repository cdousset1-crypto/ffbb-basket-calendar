from ffbb_data_client import FFBBDataClient


def main():
    print("Initialisation du client FFBB...")

    client = FFBBDataClient.create()

    print("Recherche des rencontres de basket...")

    result = client.search_rencontres("U11 M1", limit=5)

    print(f"Type de réponse : {type(result).__name__}")
    print(f"Résultat : {result}")


if __name__ == "__main__":
    main()
