from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Country, State, City


LOCATIONS = {
    "India": {
        "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Salem", "Tiruchirappalli"],
        "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Thane", "Nashik"],
        "Karnataka": ["Bengaluru", "Mysuru", "Hubli", "Mangaluru", "Belagavi"],
        "Delhi": ["New Delhi", "Dwarka", "Rohini", "Saket", "Karol Bagh"],
        "Uttar Pradesh": ["Lucknow", "Kanpur", "Agra", "Varanasi", "Prayagraj"],
        "West Bengal": ["Kolkata", "Howrah", "Durgapur", "Siliguri", "Asansol"],
        "Andhra Pradesh": ["Visakhapatnam", "Vijayawada", "Guntur", "Nellore", "Kurnool"],
        "Telangana": ["Hyderabad", "Warangal", "Nizamabad", "Karimnagar", "Ramagundam"],
        "Kerala": ["Thiruvananthapuram", "Kochi", "Kozhikode", "Thrissur", "Alappuzha"],
        "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur", "Kota", "Bikaner"],
    },
    "USA": {
        "California": ["Los Angeles", "San Francisco", "San Diego", "Sacramento", "San Jose"],
        "New York": ["New York City", "Buffalo", "Rochester", "Albany", "Syracuse"],
        "Texas": ["Houston", "Dallas", "Austin", "San Antonio", "Fort Worth"],
        "Florida": ["Miami", "Orlando", "Tampa", "Jacksonville", "Fort Lauderdale"],
        "Illinois": ["Chicago", "Springfield", "Naperville", "Aurora", "Rockford"],
    },
    "Canada": {
        "Ontario": ["Toronto", "Ottawa", "Mississauga", "Hamilton", "London"],
        "British Columbia": ["Vancouver", "Victoria", "Surrey", "Burnaby", "Richmond"],
        "Quebec": ["Montreal", "Quebec City", "Laval", "Gatineau", "Longueuil"],
        "Alberta": ["Calgary", "Edmonton", "Red Deer", "Lethbridge", "St. Albert"],
        "Manitoba": ["Winnipeg", "Brandon", "Thompson", "Portage la Prairie", "Steinbach"],
    },
    "United Kingdom": {
        "England": ["London", "Birmingham", "Manchester", "Liverpool", "Leeds"],
        "Scotland": ["Edinburgh", "Glasgow", "Aberdeen", "Dundee", "Inverness"],
        "Wales": ["Cardiff", "Swansea", "Newport", "Bangor", "Wrexham"],
        "Northern Ireland": ["Belfast", "Derry", "Lisburn", "Newry", "Armagh"],
    },
    "Australia": {
        "New South Wales": ["Sydney", "Newcastle", "Wollongong", "Central Coast"],
        "Victoria": ["Melbourne", "Geelong", "Ballarat", "Bendigo"],
        "Queensland": ["Brisbane", "Gold Coast", "Sunshine Coast", "Townsville"],
        "Western Australia": ["Perth", "Fremantle", "Bunbury", "Albany"],
    },
    "Russia": {
        "Moscow": ["Moscow City", "Zelenograd", "Troitsk", "Shcherbinka", "Krasnogorsk"],
        "Saint Petersburg": ["Saint Petersburg", "Pushkin", "Peterhof", "Kronstadt", "Kolpino"],
        "Tatarstan": ["Kazan", "Naberezhnye Chelny", "Almetyevsk", "Zelenodolsk", "Bugulma"],
        "Sverdlovsk": ["Yekaterinburg", "Nizhny Tagil", "Kamensk-Uralsky", "Pervouralsk", "Serov"],
    },
    "Japan": {
        "Tokyo": ["Tokyo City", "Hachioji", "Machida", "Tachikawa", "Fuchu"],
        "Osaka": ["Osaka City", "Sakai", "Higashiosaka", "Toyonaka", "Hirakata"],
        "Kanagawa": ["Yokohama", "Kawasaki", "Sagamihara", "Fujisawa", "Yokosuka"],
        "Aichi": ["Nagoya", "Toyohashi", "Okazaki", "Ichinomiya", "Toyota"],
    },
    "China": {
        "Beijing": ["Beijing City", "Haidian", "Chaoyang", "Fengtai", "Tongzhou"],
        "Shanghai": ["Shanghai City", "Pudong", "Minhang", "Baoshan", "Songjiang"],
        "Guangdong": ["Guangzhou", "Shenzhen", "Dongguan", "Foshan", "Zhuhai"],
        "Zhejiang": ["Hangzhou", "Ningbo", "Wenzhou", "Shaoxing", "Jiaxing"],
    },
    "Germany": {
        "Bavaria": ["Munich", "Nuremberg", "Augsburg", "Regensburg", "Würzburg"],
        "North Rhine-Westphalia": ["Cologne", "Düsseldorf", "Dortmund", "Essen", "Bonn"],
        "Berlin": ["Berlin City", "Mitte", "Charlottenburg", "Friedrichshain", "Neukölln"],
        "Hamburg": ["Hamburg City", "Altona", "Eimsbüttel", "Harburg", "Wandsbek"],
    },
    "France": {
        "Île-de-France": ["Paris", "Boulogne-Billancourt", "Saint-Denis", "Versailles", "Créteil"],
        "Provence-Alpes-Côte d'Azur": ["Marseille", "Nice", "Toulon", "Aix-en-Provence", "Cannes"],
        "Auvergne-Rhône-Alpes": ["Lyon", "Grenoble", "Saint-Étienne", "Villeurbanne", "Valence"],
        "Nouvelle-Aquitaine": ["Bordeaux", "Limoges", "Poitiers", "Pau", "La Rochelle"],
    },
    "Italy": {
        "Lombardy": ["Milan", "Bergamo", "Brescia", "Monza", "Varese"],
        "Lazio": ["Rome", "Latina", "Guidonia", "Fiumicino", "Aprilia"],
        "Campania": ["Naples", "Salerno", "Caserta", "Avellino", "Benevento"],
        "Veneto": ["Venice", "Verona", "Padua", "Vicenza", "Treviso"],
    },
    "Spain": {
        "Madrid": ["Madrid City", "Alcalá de Henares", "Móstoles", "Getafe", "Leganés"],
        "Catalonia": ["Barcelona", "Girona", "Lleida", "Tarragona", "Badalona"],
        "Andalusia": ["Seville", "Málaga", "Granada", "Córdoba", "Almería"],
        "Valencia": ["Valencia City", "Alicante", "Elche", "Castellón", "Torrevieja"],
    },
    "Brazil": {
        "São Paulo": ["São Paulo City", "Campinas", "Santos", "São José dos Campos", "Ribeirão Preto"],
        "Rio de Janeiro": ["Rio de Janeiro City", "Niterói", "Duque de Caxias", "Nova Iguaçu", "Petrópolis"],
        "Minas Gerais": ["Belo Horizonte", "Uberlândia", "Juiz de Fora", "Montes Claros", "Divinópolis"],
        "Bahia": ["Salvador", "Feira de Santana", "Vitória da Conquista", "Ilhéus", "Lauro de Freitas"],
    },
    "South Africa": {
        "Gauteng": ["Johannesburg", "Pretoria", "Soweto", "Benoni"],
        "Western Cape": ["Cape Town", "Stellenbosch", "George", "Paarl"],
        "KwaZulu-Natal": ["Durban", "Pietermaritzburg", "Richards Bay", "Newcastle"],
    },
    "South Korea": {
        "Seoul": ["Seoul City", "Gangnam", "Songpa", "Jung-gu", "Yeongdeungpo"],
        "Gyeonggi": ["Suwon", "Goyang", "Yongin", "Seongnam", "Anyang"],
        "Busan": ["Busan City", "Haeundae", "Sasang", "Dongnae", "Nam-gu"],
        "Incheon": ["Incheon City", "Yeonsu", "Namdong", "Seo-gu", "Bupyeong"],
    },
    "Singapore": {
        "Singapore": ["Singapore Central", "Jurong", "Tampines", "Woodlands", "Pasir Ris"],
    },
    "Malaysia": {
        "Selangor": ["Shah Alam", "Petaling Jaya", "Klang", "Subang Jaya", "Ampang"],
        "Kuala Lumpur": ["Kuala Lumpur City", "Bukit Bintang", "Cheras", "Sentul", "Kepong"],
        "Johor": ["Johor Bahru", "Batu Pahat", "Muar", "Kluang", "Segamat"],
        "Penang": ["George Town", "Bayan Lepas", "Butterworth", "Bukit Mertajam", "Nibong Tebal"],
    },
    "Philippines": {
        "Metro Manila": ["Manila City", "Quezon City", "Makati", "Pasig", "Taguig"],
        "Calabarzon": ["Calamba", "Batangas City", "Lucena", "Lipa", "San Pablo"],
        "Central Visayas": ["Cebu City", "Lapu-Lapu", "Mandaue", "Dumaguete", "Toledo"],
        "Davao": ["Davao City", "Digos", "Mati", "Tagum", "Panabo"],
    },
    "Pakistan": {
        "Sindh": ["Karachi", "Hyderabad", "Sukkur", "Larkana"],
        "Punjab": ["Lahore", "Faisalabad", "Rawalpindi", "Multan"],
        "Khyber Pakhtunkhwa": ["Peshawar", "Abbottabad", "Swat", "Mardan"],
    },
    "Sri Lanka": {
        "Western": ["Colombo", "Negombo", "Kalutara", "Gampaha"],
        "Central": ["Kandy", "Matale", "Nuwara Eliya", "Dambulla"],
        "Southern": ["Galle", "Matara", "Hambantota", "Tangalle"],
    },
    "UAE": {
        "Abu Dhabi": ["Abu Dhabi City", "Al Ain", "Madinat Zayed", "Al Ruwais", "Liwa"],
        "Dubai": ["Dubai City", "Jebel Ali", "Hatta", "Al Awir", "Deira"],
        "Sharjah": ["Sharjah City", "Khor Fakkan", "Kalba", "Dibba Al-Hisn", "Al Dhaid"],
    },
    "Saudi Arabia": {
        "Riyadh": ["Riyadh City", "Al Kharj", "Al Majma'ah", "Al Zulfi", "Al Dawadmi"],
        "Mecca": ["Mecca City", "Jeddah", "Taif", "Al Qunfudhah", "Rabigh"],
        "Eastern Province": ["Dammam", "Dhahran", "Al Khobar", "Al Ahsa", "Jubail"],
    },
    "Indonesia": {
        "Jakarta": ["Jakarta City", "Jakarta Selatan", "Jakarta Timur", "Jakarta Barat", "Jakarta Utara"],
        "West Java": ["Bandung", "Bekasi", "Bogor", "Depok", "Cimahi"],
        "East Java": ["Surabaya", "Malang", "Kediri", "Madiun", "Blitar"],
        "Central Java": ["Semarang", "Solo", "Magelang", "Pekalongan", "Salatiga"],
    },
    "New Zealand": {
        "Auckland": ["Auckland City", "Manukau", "North Shore", "Waitakere"],
        "Wellington": ["Wellington City", "Lower Hutt", "Upper Hutt", "Porirua"],
        "Canterbury": ["Christchurch", "Timaru", "Ashburton", "Rangiora"],
    },
    "Netherlands": {
        "North Holland": ["Amsterdam", "Haarlem", "Zaanstad", "Hilversum", "Alkmaar"],
        "South Holland": ["Rotterdam", "The Hague", "Leiden", "Dordrecht", "Delft"],
        "Utrecht": ["Utrecht City", "Amersfoort", "Nieuwegein", "Zeist", "Soest"],
        "North Brabant": ["Eindhoven", "Tilburg", "Breda", "'s-Hertogenbosch", "Helmond"],
    },
    "Sweden": {
        "Stockholm": ["Stockholm City", "Södertälje", "Täby", "Tumba", "Upplands Väsby"],
        "Västra Götaland": ["Gothenburg", "Borås", "Trollhättan", "Skövde", "Uddevalla"],
        "Skåne": ["Malmö", "Helsingborg", "Lund", "Kristianstad", "Landskrona"],
        "Uppsala": ["Uppsala City", "Enköping", "Knivsta", "Östhammar", "Bålsta"],
    },
    "Norway": {
        "Oslo": ["Oslo City", "Bærum", "Asker", "Lillestrøm", "Lørenskog"],
        "Vestland": ["Bergen", "Stord", "Jørpeland", "Odda", "Leirvik"],
        "Trøndelag": ["Trondheim", "Stjørdal", "Levanger", "Namsos", "Melhus"],
        "Rogaland": ["Stavanger", "Sandnes", "Haugesund", "Egersund", "Sola"],
    },
    "Denmark": {
        "Capital Region": ["Copenhagen", "Frederiksberg", "Gentofte", "Helsingør", "Hillerød"],
        "Central Jutland": ["Aarhus", "Randers", "Silkeborg", "Horsens", "Herning"],
        "Southern Denmark": ["Odense", "Esbjerg", "Kolding", "Vejle", "Sønderborg"],
    },
    "Switzerland": {
        "Zürich": ["Zürich City", "Winterthur", "Uster", "Dübendorf", "Dietikon"],
        "Bern": ["Bern City", "Biel", "Thun", "Köniz", "Spiez"],
        "Geneva": ["Geneva City", "Carouge", "Vernier", "Lancy", "Meyrin"],
        "Vaud": ["Lausanne", "Nyon", "Montreux", "Yverdon-les-Bains", "Vevey"],
    },
}


async def seed_locations(db: AsyncSession):
    from sqlalchemy import select, func

    result = await db.execute(select(func.count(Country.id)))
    count = result.scalar()
    if count > 0:
        return

    for country_name, states in LOCATIONS.items():
        country = Country(name=country_name)
        db.add(country)
        await db.flush()

        for state_name, cities in states.items():
            state = State(name=state_name, country_id=country.id)
            db.add(state)
            await db.flush()

            for city_name in cities:
                city = City(name=city_name, state_id=state.id)
                db.add(city)

    await db.commit()
