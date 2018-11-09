import json
import csv
import os
from argparse import ArgumentParser
from tabulate import tabulate
import folium
import pandas as pd
import datetime
import requests
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt


def query_github(github_user, github_pw, git_repo_url_base, https_proxy=None, fname="star_gazers.csv"):
    headers = {'Accept': 'application/vnd.github.v3.star+json'}
    proxyDict = {"https": https_proxy}
    recs_per_page = 50
    page = 1
    cnt_stars = 0
    git_repo_url_base = git_repo_url_base.replace("github.com", "api.github.com/repos")
    with open(fname, "w") as file:
        csv_file = csv.writer(file)
        while True:
            git_repo_url = git_repo_url_base + "/stargazers?page={}&per_page={}"
            r = requests.get(git_repo_url.format(page, recs_per_page),
                             auth=(github_user, github_pw),
                             headers=headers, proxies=proxyDict)
            if not r.ok:
                print("Done or Error")
                break
            stars_records = json.loads(r.text or r.content)
            nstars = len(stars_records)
            if nstars <= 0:
                print("Done")
                break
            for star in range(nstars):
                print("-" * 50 + str(cnt_stars+star) + " (page:" + str(page) + ") " + "-" * 50)
                print(stars_records[star])
                user = stars_records[star]['user']
                login = user['login']
                starred_at = stars_records[star]['starred_at']
                starred = datetime.datetime.strptime(starred_at, '%Y-%m-%dT%H:%M:%SZ')
                print(login, starred.year, starred.day, starred.month)

                r = requests.get("https://api.github.com/users/" + login,
                                 auth=(github_user, github_pw),
                                 headers=headers, proxies=proxyDict)
                if not r.ok:
                    raise ValueError("GET https://api.github.com/users/ failed")
                user_desc = json.loads(r.text or r.content)
                for k,v in user_desc.items():
                    if isinstance(v, str):
                        user_desc[k] = v.encode('utf-8')
                print(user_desc['login'], user_desc['id'], user_desc['company'],
                      user_desc['name'], user_desc['location'], user_desc['bio'], starred_at)
                csv_file.writerow([user_desc['login'], user_desc['id'], user_desc['company'],
                                   user_desc['name'], user_desc['location'], user_desc['bio'], starred_at])
                file.flush()
                os.fsync(file.fileno())
            page += 1
            cnt_stars += nstars

    print("Total: ", cnt_stars)


def get_countries_metadata(fname="countries-readable.json"):
    """Read a database file containing information about different countries.

    Specifically, we are interested in the capital and population of each country, because
    we will use this information to disambiguate records, by giving more weight to certain
    features.
    Source: https://github.com/lorey/list-of-countries/blob/master/json/countries-readable.json
    """
    details = {}
    with open(fname) as f:
        countries = json.load(f)
    for country in countries:
        details[country['name'].lower()] = (country['capital'].lower(), int(country['population']))

    # We replace "South Korea" by "Korea" since "Korea" is a shorthand form many people use.  I'm going to make a
    # political statement and say that unfortunately North Korea is not relevant to github for the time being.
    details['korea'] = details['south korea']
    del details['south korea']
    return details


def read_cities_db(fname="world-cities_json.json"):
    """Read a database file containing names of cities from different countries.

    Source: https://pkgstore.datahub.io/core/world-cities/world-cities_json/data/5b3dd46ad10990bca47b04b4739a02ba/world-cities_json.json
    """
    with open(fname) as f:
        world_cities = json.load(f)

    country_city_pairs = set()
    processed_sub_countries = []
    for city_record in world_cities:
        country = city_record['country'].lower()
        if country == "south korea":
            # See my comment above regarding the special handling of South Korea
            country = "korea"
        city = city_record['name'].lower()
        country_city_pairs.add((country, city))
        subcountry = city_record['subcountry'].lower() if city_record['subcountry'] is not None else None
        if subcountry is not None and subcountry not in processed_sub_countries:
            # Add (subcountry, country)
            processed_sub_countries.append(subcountry)
            country_city_pairs.add((country, subcountry))

    # People use these abbreviations, so we can't ignore them
    country_city_pairs.add(('united states', 'usa'))
    country_city_pairs.add(('united states', 'u.s.a.'))
    country_city_pairs.add(('united kingdom', 'uk'))
    country_city_pairs.add(('united kingdom', 'u.k.'))
    country_city_pairs.add(('china', 'prc'))
    country_city_pairs.add(('china', 'p.r.c.'))

    # Sort by longest city name first, because later we want to do long-string-match
    country_city_pairs = sorted(country_city_pairs, key=lambda pair: len(pair[1]), reverse=True)
    return country_city_pairs


def get_location_feature(record):
    """Clean the star record, and return the location feature.
    """
    location_features = record[4]
    location_features = location_features.lower()
    location_features = location_features.replace('\n', ' ')
    location_features = location_features.replace(',', ' ')
    location_features = location_features.replace('，', ' ')
    return location_features


def match_country(raw_location_feature, country_city_pairs, country_details):
    """Find the country which is most-likely home of this github star-gazer.

    We perform a simple search for the names of countries and cities.

    Return:
        matched country, matched city, string describing how we matched (for debug)
    """
    matches = []

    for (country, city) in country_city_pairs:
        if city in raw_location_feature:
            # So we only append to the matches list if the length of the matched city is
            # as long as the existing matches.  We rely on the fact that when we created
            # country_city_pairs, we sorted it by the longest-city-name-first.
            # This is a form of disambiguation: we only want to consider the longest matches.
            # Some city names are really short (e.g. 2 letters) and will be falsly detected,
            # so we only consider them a match if we didn't match a longer substring.
            if len(matches) == 0 or len(city) == len(matches[0][1]):
                matches.append((country, city))

    for country in country_details.keys():
        if country in raw_location_feature:
            # This is a simple way to give this signal extra weight:
            # Matching a country name is a very strong indication.
            matches.append((country, country))
            matches.append((country, country))

    if len(matches) == 0:
        return None, None, "No match found"
    if len(matches) == 1:
        return matches[0][0], matches[0][1], "Single match found ({})".format(matches[0][1])

    # Multiple matches - requires disambiguation
    # 1. Use voting to disambiguate: we create a dictionary for counting how many matches
    # we found for each country.
    candidate_countries = {}
    for (country, city) in matches:
        try:
            candidate_countries[country] += 1
        except KeyError:
            candidate_countries[country] = 1
    if len(candidate_countries) == 1:
        # Only one country is in the matches list
        return matches[0][0], matches[0][1], "Multiple match for a single country"

    #print("Ambiguity for {}: {}".format(record, matches))
    # Sort the dictionary by the number of matches per country
    candidate_list = sorted(candidate_countries.items(), key=lambda kv: kv[1], reverse=True)
    if candidate_list[0][1] > candidate_list[1][1]:
        # One country dominates
        return candidate_list[0][0], candidate_list[0][0], "Most matches"

    # Ladies and gentlemen: we have a tie!
    # Two or more countries have the same number of matches.

    # 1. Disambiguate by giving more weight to country capitals: if we find that in
    # the matches list we have both a country and its capital, we consider that a
    # very strong signal of a correct match.
    largest_population = {'country_city': (None, None), 'population': 0}
    for (country, city) in matches:
        if city == country_details[country][0]:
            # Found a match with a capital of a country
            return country, city, "Resolved ambiguity by matching the capital"
        # We also look for the country with the largest population (see below).
        population = country_details[country][1]
        if population > largest_population['population']:
            largest_population['country_city'] = (country, city)
            largest_population['population'] = population

    # 2. Disambiguate by giving more weight to the country with the larger population.
    # The idea is that all-else-being-equal, it is more likely that a star came from a
    # country with a larger population.
    return (largest_population['country_city'][0],
            largest_population['country_city'][1],
            "Resolved ambiguity by population " + str(matches))


# Use this flag to debug the parsing of star-gazer country-of-origin decision.
# Set this DEBUG_COUNTRY to the name of a particual country (all small caps) and
# you will see how and why it made its classification decision.
DEBUG_COUNTRY = None
#DEBUG_COUNTRY = "brazil"


def read_starring_history_db(country_city_pairs, country_details, fcache):
    countries_stats = {}
    with open(fcache) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        record_count = 0
        for record in csv_reader:
            raw_location_feature = get_location_feature(record)
            matched_country, matched_str, reason = match_country(raw_location_feature,
                                                                 country_city_pairs,
                                                                 country_details)
            if matched_country is not None:
                if matched_country == DEBUG_COUNTRY:
                    print("Detected {} in: {}  matched: {}  reason: {}".format(matched_country, raw_location_feature,
                                                                               matched_str, reason))
                try:
                    countries_stats[matched_country]["count"] += 1
                    countries_stats[matched_country]["records"].append(record_count)
                    countries_stats[matched_country]["debug"].append(raw_location_feature)
                except KeyError:
                    countries_stats[matched_country] = {"count": 1,
                                                        "records": [record_count],
                                                        "debug": [raw_location_feature]}
            record_count += 1
        return countries_stats, record_count


def cached_query_results_summary(fcache):
    country_details = get_countries_metadata()
    country_city_pairs = read_cities_db()
    countries_stats, record_count = read_starring_history_db(country_city_pairs, country_details, fcache)
    print("\nSummary:")
    print("Total: ", record_count)
    total_matches = 0
    for country_name, country_match_info in countries_stats.items():
        cnt = country_match_info["count"]
        total_matches += cnt
    print("total_matches = ", total_matches)

    verbose = False
    if verbose:
        print(json.dumps(countries_stats, indent=4))

    countries_list = sorted(countries_stats.items(), key=lambda kv: kv[1]["count"], reverse=True)
    return countries_list, record_count, total_matches


def cached_query_results_df(fcache):
    countries_list, record_count, total_matches = cached_query_results_summary(fcache)
    df = pd.DataFrame(columns=['Country', 'Instances', '%', '% extrapolated'])
    for country_stats in countries_list:
        country = country_stats[0]
        cnt_instances = country_stats[1]["count"]
        df.loc[len(df.index)] = ([country,
                                  cnt_instances,
                                  100 * cnt_instances/record_count,
                                  100 * cnt_instances/total_matches])
    return df


def list_stars_per_country(fcache):
    df = cached_query_results_df(fcache)
    t = tabulate(df, headers='keys', tablefmt='psql', floatfmt=".5f")
    print(t)


def create_stars_map(fcache, html_name='stars_map.html'):
    # Source https://github.com/albertyw/avenews/blob/master/old/data/average-latitude-longitude-countries.csv
    geo = {}
    fname = "average-latitude-longitude-countries.csv"
    with open(fname) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        next(csv_reader, None)  # skip the headers
        for record in csv_reader:
            country = record[1].lower()
            if country == "korea, democratic people's republic of":
                country = "korea"
            if country == "russian federation":
                country = "russia"
            if country == "moldova, republic of":
                country = "moldova"
            if country == "iran, islamic republic of":
                country = "iran"
            if country == "canada":
                # I don't like the coordinates chosen for Canada
                geo[country] = (float(record[2])-4, float(record[3])-2)
                continue
            geo[country] = (float(record[2]), float(record[3]))
    # Add missing record for Ivory Coast
    geo["ivory coast"] = (8, 6)
    countries_list, record_count, total_matches = cached_query_results_summary(fcache)

    # Make an empty map
    m = folium.Map(location=[20, 0], tiles="Mapbox Bright", zoom_start=2)

    MAX_RADIUS = 3000000

    # Add marker one by one on the map
    for country_stats in countries_list:
        try:
            country = country_stats[0]
            cnt_instances = country_stats[1]["count"]
            folium.Circle(
              location=[geo[country][0], geo[country][1]],
              popup="{}: {:.2f}%".format(country, cnt_instances*100/total_matches),
              radius=MAX_RADIUS * cnt_instances/total_matches,
              color='crimson',
              fill=True,
              fill_color='crimson'
            ).add_to(m)
        except KeyError as e:
            # Misclassification of strings as valid country names can occur,
            # although they should be very few, if any.
            print(e)

    # Save it as html
    m.save(html_name)
    print("Created HTML file {}".format(html_name))


def add_star_for_date(record, starring_log):
    """Add a star to the stars-count of the date of the specified log record.

    Convert from string, to datetime.datetime to datetime.date.
    """
    starred_at_str = record[6]
    starred_at_datetime = datetime.datetime.strptime(starred_at_str, '%Y-%m-%dT%H:%M:%SZ')
    starred_at_date = datetime.date(starred_at_datetime.year,
                                    starred_at_datetime.month,
                                    starred_at_datetime.day)
    starring_log[starred_at_date] = starring_log.setdefault(starred_at_date, 0) + 1


def group_by_date_df(fcache, group_type):
    """Monthly trending stars data"""
    assert group_type in ["monthly", "daily"]

    starring_log = {}
    with open(fcache) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        for record in csv_reader:
            add_star_for_date(record, starring_log)

    group = {}
    for date, cnt in starring_log.items():
        if group_type == "monthly":
            key = str(date.month) + "/" + str(date.year)
        elif group_type == "daily":
            key = date
        group[key] = group.setdefault(key, 0) + cnt
    total = 0
    if group_type == "monthly":
        df = pd.DataFrame(columns=['Month', 'New Stars', 'Cumulative Stars'])
    else:
        df = pd.DataFrame(columns=['Date', 'New Stars', 'Cumulative Stars'])
    for month, cnt in group.items():
        total += cnt
        df.loc[len(df.index)] = ([month, cnt, total])
    return df


def print_history(fcache, group_type):
    """Monthly trending stars data"""
    df = group_by_date_df(fcache, group_type)
    t = tabulate(df, headers='keys', tablefmt='psql', floatfmt=".5f")
    print(t)


def plot_history(fcache, group_type):
    """Monthly trending stars data"""
    df = group_by_date_df(fcache, group_type)
    if group_type == "monthly":
        plt.plot(df['Month'], df['New Stars'], marker='o', markerfacecolor='blue', markersize=8, color='skyblue', linewidth=3)
    else:
        plt.plot(df['Date'], df['New Stars'], marker='o', markerfacecolor='blue', markersize=8, color='skyblue',
                 linewidth=3)
    #plt.title('New stars activity for ' + start_of_month.strftime("%B/%Y"))
    plt.xticks(rotation=90)
    plt.ylabel('Stars');
    plt.show()


def daily_history_df(fcache, desired_month, desired_year):
    """Daily trending stars data"""
    starring_log = {}
    with open(fcache) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter=',')
        for record in csv_reader:
            add_star_for_date(record, starring_log)

    daily = {}
    total = 0
    start_of_month = datetime.date(desired_year, desired_month, 1)
    for date, cnt in starring_log.items():
        if date < start_of_month:
            total += cnt
        if date.month == desired_month:
            key = str(date.day) + "/" + str(date.month)
            daily[key] = daily.setdefault(key, 0) + cnt

    df = pd.DataFrame(columns=['Date', 'New Stars', 'Cumulative Stars'])
    for month, cnt in daily.items():
        total += cnt
        df.loc[len(df.index)] = ([month, cnt, total])
    return df


def print_daily_history(fcache, desired_month=9, desired_year=2018):
    df = daily_history_df(fcache, desired_month, desired_year)
    t = tabulate(df, headers='keys', tablefmt='psql', floatfmt=".5f")
    print(t)


def plot_daily_history(fcache, desired_month=6, desired_year=2018):
    df = daily_history_df(fcache, desired_month, desired_year)
    plt.plot(df['Date'], df['New Stars'], marker='o', markerfacecolor='blue', markersize=8, color='skyblue', linewidth=3)
    start_of_month = datetime.date(desired_year, desired_month, 1)
    plt.title('New stars activity for ' + start_of_month.strftime("%B/%Y"))
    plt.xticks(rotation=90)
    plt.ylabel('Stars');
    plt.show()


def add_star_for_day_of_week(record, starring_log):
    starred_at = record[6]
    starred = datetime.datetime.strptime(starred_at, '%Y-%m-%dT%H:%M:%SZ')
    day_of_week = starred.weekday()
    starring_log[day_of_week] = starring_log.setdefault(day_of_week, 0) + 1


# def print_day_of_week_history(fcache):
#     """Print the number of stars per each day of the week"""
#     daily_log = {}
#     day_of_week_log = {}
#     with open(fcache) as csv_file:
#         csv_reader = csv.reader(csv_file, delimiter=',')
#         for record in csv_reader:
#             add_star_for_day_of_week(record, day_of_week_log)
#             add_star_for_date(record, daily_log)
#
#     # Remove outliers.
#     # Certain events, like an announcement over social media, can cause a daily peak that is an outlier and
#     # not indicative of the steady-state star-gazers behavior.  We can remove these events, to get a "cleaner"
#     # view of the gazers behavior.  You might have to look at the data and experiment.
#     # In this case, I only remove the highest single-day starring event.
#     daily_log = sorted(daily_log.items(), key=lambda kv: kv[1], reverse=True)
#     outlier_date = daily_log[0][0]
#     outlier_val = daily_log[0][1]
#     outlier_date = datetime.datetime(outlier_date)
#     day_of_week_log[outlier_date.weekday()] -= outlier_val
#     print(day_of_week_log)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument('command',
                        choices=["query-github",
                                 "stars-geo-map",
                                 "stars-geo-tbl",
                                 "monthly",
                                 "daily",
                                 #"day-of-week",
                                 "detailed-month"],
                        help='path to dataset')
    parser.add_argument("-u", "--user", dest="git_user", help="git user name")
    parser.add_argument("-p", "--password", dest="git_pw", help="git user password")
    parser.add_argument("-x", "--proxy", dest="proxy", help="HTTPS proxy", default=None)
    parser.add_argument("-r", "--repo",
                        dest="git_repo",
                        help="git repo URL (e.g. https://github.com/NervanaSystems/distiller)")
    parser.add_argument("-c", "--cache-file", dest="cache_file", default="star_gazers.csv",
                        help="path to the file caching the results of querying github")
    parser.add_argument("-f", "--format",
                        choices=["plot","console"],
                        default="console",
                        dest="output_format",
                        help="output format: plot|console")
    args = parser.parse_args()
    if args.command == "query-github":
        query_github(args.git_user, args.git_pw, args.git_repo, args.proxy)
    if args.command == "stars-geo-tbl":
        list_stars_per_country(args.cache_file)
    if args.command == "stars-geo-map":
        create_stars_map(args.cache_file)
    if args.command == "monthly" or args.command == "daily":
        if args.output_format == "plot":
            plot_history(args.cache_file, group_type=args.command)
        else:
            print_history(args.cache_file, group_type=args.command)
    if args.command == "detailed-month":
        if args.output_format == "plot":
            plot_daily_history(args.cache_file)
        else:
            print_daily_history(args.cache_file)
    # if args.command == "day-of-week":
    #     print_day_of_week_history(args.cache_file)
