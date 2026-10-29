#!/usr/bin/env python3
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
BACKUP_DIR = ROOT / "backups" / "data-fixes"

WIKI_SOURCE = "https://en.wikipedia.org/wiki/List_of_government_schools_in_Victoria,_Australia"


def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def normalized_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return "https://" + url


def backup_database():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"aaie_crm_before_school_contact_field_backfill_{stamp}.db"
    shutil.copy2(DB_PATH, target)
    return target


VIC_WIKI_ROWS = {
    "Abbotsford Primary School": ("Abbotsford", "3067", "www.abbotsfordps.vic.edu.au"),
    "Albany Rise Primary School": ("Mulgrave", "3170", "www.albanyrise-ps.vic.edu.au"),
    "Araluen Primary School": ("Sale", "3850", "www.araluenps.vic.edu.au"),
    "Armadale Primary School": ("Armadale", "3143", "armadaleps.vic.edu.au"),
    "Auburn South Primary School": ("Hawthorn East", "3123", "www.auburnsthps.vic.edu.au"),
    "Avondale Primary School": ("Avondale Heights", "3034", "www.avondaleps.vic.edu.au"),
    "Balwyn Primary School": ("Balwyn", "3103", "www.balwynps.vic.edu.au"),
    "Blackburn Primary School": ("Blackburn", "3130", "www.blackburnps.vic.edu.au"),
    "Boroondara Park Primary School": ("Balwyn North", "3104", "www.bppps.vic.edu.au"),
    "Box Hill North Primary School": ("Box Hill North", "3129", "www.bhnps.vic.edu.au"),
    "Brentwood Park Primary School": ("Berwick", "3806", "www.brentwoodparkps.vic.edu.au"),
    "Bridgewood Primary School": ("Officer", "3809", "www.bridgewoodps.vic.edu.au"),
    "Brighton Primary School": ("Brighton", "3186", "www.brighton.vic.edu.au"),
    "Brunswick South Primary School": ("Brunswick East", "3057", "www.brunswicksouthps.vic.edu.au"),
    "Brunswick South West Primary School": ("Brunswick West", "3055", "www.bswps.vic.edu.au"),
    "Burwood East Primary School": ("Burwood East", "3151", "www.burwoodeastps.vic.edu.au"),
    "Burwood Heights Primary School": ("Burwood East", "3151", "www.burwoodhps.vic.edu.au"),
    "Carrington Primary School": ("Knoxfield", "3180", "www.carringtonps.vic.edu.au"),
    "Chandler Park Primary School": ("Keysborough", "3173", "chandlerparkps.vic.edu.au"),
    "Clarinda Primary School": ("Clarinda", "3169", "www.clarindaps.vic.edu.au"),
    "Clayton South Primary School": ("Clayton South", "3169", "www.claytonsouthps.vic.edu.au"),
    "Donburn Primary School": ("Doncaster East", "3109", "donburnps.vic.edu.au"),
    "Doncaster Gardens Primary School": ("Doncaster East", "3109", "www.dgps.vic.edu.au"),
    "Doncaster Primary School": ("Doncaster", "3108", "www.doncasterps.vic.edu.au"),
    "Donvale Primary School": ("Donvale", "3111", "donvaleps.vic.edu.au"),
    "Elsternwick Primary School": ("Brighton", "3186", "www.elsternwickps.vic.edu.au"),
    "Frankston Primary School": ("Frankston", "3199", "www.frankstonps.vic.edu.au"),
    "Glen Waverley Primary School": ("Glen Waverley", "3150", "www.glenps.vic.edu.au"),
    "Glen Waverley South Primary School": ("Glen Waverley", "3150", "www.gwsps.vic.edu.au"),
    "Glenferrie Primary School": ("Hawthorn", "3122", "glenferrieps.vic.edu.au"),
    "Glengala Primary School": ("Sunshine West", "3020", "www.glengala.vic.edu.au"),
    "Glenroy West Primary School": ("Glenroy", "3046", "www.glenroywestps.vic.edu.au"),
    "Greensborough Primary School": ("Greensborough", "3088", "greensborough.vic.edu.au"),
    "Greythorn Primary School": ("Balwyn North", "3104", "www.greythornps.vic.edu.au"),
    "Heatherhill Primary School": ("Springvale", "3171", "www.heatherhillps.vic.edu.au"),
    "Highvale Primary School": ("Glen Waverley", "3150", "www.highvaleps.vic.edu.au"),
    "Huntingdale Primary School": ("Oakleigh South", "3167", "huntingdaleps.vic.edu.au"),
    "Irymple Primary School": ("Irymple", "3498", "www.irympleps.vic.edu.au"),
    "Kerrimuir Primary School": ("Box Hill North", "3129", "www.kerrimuirps.vic.edu.au"),
    "Kew Primary School": ("Kew", "3101", "www.kewps.vic.edu.au"),
    "Kingsbury Primary School": ("Kingsbury", "3083", "www.kingsburyps.vic.edu.au"),
    "Laburnum Primary School": ("Blackburn", "3130", "www.laburnumps.vic.edu.au"),
    "Lalor Gardens Primary School": ("Lalor", "3075", "www.lalorgardensps.vic.edu.au"),
    "Livingstone Primary School": ("Vermont South", "3133", "www.livingstoneps.vic.edu.au"),
    "Malvern Primary School": ("Malvern East", "3145", "www.malvernps.vic.edu.au"),
    "Malvern Valley Primary School": ("Malvern East", "3145", "mvps.vic.edu.au"),
    "McKinnon Primary School": ("Ormond", "3204", "mckinnon-primary.vic.edu.au"),
    "Mernda Park Primary School": ("Mernda", "3754", "www.merndaparkps.vic.edu.au"),
    "Milgate Primary School": ("Doncaster East", "3109", "www.milgateps.vic.edu.au"),
    "Mitcham Primary School": ("Mitcham", "3132", "www.mitcham.ps.vic.edu.au"),
    "Mont Albert Primary School": ("Mont Albert", "3127", "www.maps.vic.edu.au"),
    "Mount Waverley Heights Primary School": ("Mount Waverley", "3149", "www.mountwaverleyheightsps.vic.edu.au"),
    "Mount Waverley North Primary School": ("Mount Waverley", "3149", "www.mtwavnth.vic.edu.au"),
    "Newtown Primary School": ("Newtown", "3220", "newtownps.vic.edu.au"),
    "Noble Park Primary School": ("Noble Park", "3174", "www.nobleparkps.vic.edu.au"),
    "Oak Park Primary School": ("Oak Park", "3046", "www.oakparkps.vic.edu.au"),
    "Old Orchard Primary School": ("Blackburn North", "3130", "www.oldorchardps.vic.edu.au"),
    "Parkmore Primary School": ("Forest Hill", "3131", "www.parkmore.vic.edu.au"),
    "Pembroke Primary School": ("Mooroolbark", "3138", "www.pemprim.vic.edu.au"),
    "Porepunkah Primary School": ("Porepunkah", "3740", "www.porepunkahps.vic.edu.au"),
    "Preston North East Primary School": ("Preston", "3072", "www.pneps.vic.edu.au"),
    "Richmond West Primary School": ("Richmond", "3121", "richmondwestps.vic.edu.au"),
    "Rosanna Golf Links Primary School": ("Rosanna", "3084", "www.rosanna-golflinks-ps.vic.edu.au"),
    "Rowville Primary School": ("Rowville", "3178", "www.rowvilleps.vic.edu.au"),
    "Sandringham East Primary School": ("Sandringham", "3191", "www.sandringhameastps.vic.edu.au"),
    "Seaholme Primary School": ("Seaholme", "3018", "www.seaholmeps.vic.edu.au"),
    "Serpell Primary School": ("Templestowe", "3106", "serpellps.vic.edu.au"),
    "Silverton Primary School": ("Noble Park North", "3174", "www.silverton-ps.vic.edu.au"),
    "Spring Parks Primary School": ("Springvale", "3171", "springparksps.vic.edu.au"),
    "Springvale Rise Primary School": ("Springvale", "3171", "springvaleriseps.vic.edu.au"),
    "Sunshine Harvester Primary School": ("Sunshine", "3020", "www.sunshineharvesterps.vic.edu.au"),
    "Syndal South Primary School": ("Mount Waverley", "3149", "www.syndalsp.vic.edu.au"),
    "Upper Ferntree Gully Primary School": ("Upper Ferntree Gully", "3156", "www.uppergullyps.vic.edu.au"),
    "Wantirna Primary School": ("Wantirna", "3152", "www.wantirnaprimary.vic.edu.au"),
    "Wattle Park Primary School": ("Burwood", "3125", "www.wattleparkps.vic.edu.au"),
    "Waverley Meadows Primary School": ("Wheelers Hill", "3150", "www.waverleymeadowsps.vic.edu.au"),
    "Westall Primary School": ("Clayton South", "3169", "www.westallps.vic.edu.au"),
    "Yarra Primary School": ("Richmond", "3121", "www.yarraps.vic.edu.au"),
}

# Only rows whose street/campus address was directly confirmed from an official school page
# or from user-provided data already present in this workspace.
VERIFIED_ADDRESSES = {
    "Abbotsford Primary School": ("20 Lithgow St, Abbotsford Victoria 3067", "https://www.abbotsfordps.vic.edu.au/"),
    "Araluen Primary School": ("81-99 Patten Street, Sale, Victoria 3850", "https://www.araluenps.vic.edu.au/"),
    "Armadale Primary School": ("9-23 Densham Road, Armadale VIC 3143", "https://armadaleps.vic.edu.au/"),
    "Boroondara Park Primary School": ("Almond Street, Balwyn North, Victoria 3104", "https://www.bppps.vic.edu.au/"),
    "Oakbank Area School": ("154 Onkaparinga Valley Road, Oakbank SA 5243", "https://oakbank.sa.edu.au/"),
    "Paralowie R-12 School": ("168-186 Whites Road, Paralowie SA 5108", "https://paralowie.sa.edu.au/"),
}

DIRECT_ROWS = {
    "Oakbank Area School": {
        "state": "SA",
        "suburb": "Oakbank",
        "postcode": "5243",
        "website_url": "https://oakbank.sa.edu.au/",
    },
    "Paralowie R-12 School": {
        "state": "SA",
        "suburb": "Paralowie",
        "postcode": "5108",
        "website_url": "https://paralowie.sa.edu.au/",
    },
    "Kenmore State School": {
        "state": "QLD",
        "suburb": "Kenmore",
        "postcode": "4069",
        "address": "2052 Moggill Road, Kenmore QLD 4069",
        "website_url": "https://kenmoress.eq.edu.au",
    },
    "Miami State High School": {
        "state": "QLD",
        "suburb": "Miami",
        "postcode": "4220",
        "address": "2137-2205 Gold Coast Highway, Miami QLD 4220",
        "website_url": "https://miamishs.eq.edu.au",
    },
    "The Gap State High School": {
        "state": "QLD",
        "suburb": "The Gap",
        "postcode": "4061",
        "address": "1020 Waterworks Road, The Gap QLD 4061",
        "website_url": "https://thegapshs.eq.edu.au",
    },
    "Upper Mount Gravatt State School": {
        "state": "QLD",
        "suburb": "Upper Mount Gravatt",
        "postcode": "4122",
        "address": "1899 Logan Road, Upper Mount Gravatt QLD 4122",
        "website_url": "https://upmtgravss.eq.edu.au",
    },
    "Melbourne High School": {
        "state": "VIC",
        "suburb": "South Yarra",
        "postcode": "3141",
        "address": "Forrest Hill, South Yarra VIC 3141",
        "website_url": "https://www.mhs.vic.edu.au/",
    },
    "Presbyterian Ladies' College": {
        "state": "VIC",
        "suburb": "Burwood",
        "postcode": "3125",
        "address": "141 Burwood Highway, Burwood VIC 3125",
        "website_url": "https://www.plc.vic.edu.au/",
    },
    "Caulfield Grammar School": {
        "state": "VIC",
        "suburb": "Caulfield",
        "postcode": "3162",
        "website_url": "https://www.caulfieldgs.vic.edu.au/",
    },
    "Melbourne Grammar School": {
        "state": "VIC",
        "suburb": "South Yarra",
        "postcode": "3141",
        "website_url": "https://www.mgs.vic.edu.au/",
    },
}


def append_note(existing, addition):
    existing = (existing or "").strip()
    if addition in existing:
        return existing
    return (existing + "\n" + addition).strip() if existing else addition


def main():
    backup = backup_database()
    stamp = now_iso()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    changed = []
    unresolved_address = []
    try:
        for school_name, (suburb, postcode, website) in VIC_WIKI_ROWS.items():
            row = conn.execute("SELECT * FROM schools WHERE school_name=? AND state='VIC'", (school_name,)).fetchone()
            if not row:
                continue
            values = {
                "suburb": suburb,
                "postcode": postcode,
                "website_url": normalized_url(website),
                "school_data_source_url": WIKI_SOURCE,
                "updated_at": stamp,
                "last_updated_at": stamp,
                "notes": append_note(row["notes"], "Contact fields backfilled from Victorian government schools list; street address kept blank unless official school page was verified."),
            }
            if school_name in VERIFIED_ADDRESSES:
                values["address"] = VERIFIED_ADDRESSES[school_name][0]
                values["school_data_source_url"] = VERIFIED_ADDRESSES[school_name][1]
            elif not (row["address"] or "").strip():
                unresolved_address.append((row["id"], school_name, suburb, postcode, normalized_url(website)))
            assignments = ",".join(f"{key}=?" for key in values)
            conn.execute(f"UPDATE schools SET {assignments} WHERE id=?", (*values.values(), row["id"]))
            changed.append((row["id"], school_name))

        for school_name, fields in DIRECT_ROWS.items():
            row = conn.execute("SELECT * FROM schools WHERE school_name=?", (school_name,)).fetchone()
            if not row:
                continue
            values = {
                "suburb": fields["suburb"],
                "postcode": fields["postcode"],
                "website_url": normalized_url(fields["website_url"]),
                "updated_at": stamp,
                "last_updated_at": stamp,
                "school_data_source_url": fields["website_url"],
                "notes": append_note(row["notes"], "Contact fields backfilled from existing project data and verified contact/source information."),
            }
            address = fields.get("address") or VERIFIED_ADDRESSES.get(school_name, ("", ""))[0]
            if address:
                values["address"] = address
            assignments = ",".join(f"{key}=?" for key in values)
            conn.execute(f"UPDATE schools SET {assignments} WHERE id=?", (*values.values(), row["id"]))
            changed.append((row["id"], school_name))

        conn.commit()
    finally:
        conn.close()

    report = ROOT / "cache" / "school_contact_backfill_unresolved_address.csv"
    with report.open("w", encoding="utf-8") as f:
        f.write("school_id,school_name,suburb,postcode,website_url\n")
        for item in unresolved_address:
            f.write(",".join(str(part).replace(",", " ") for part in item) + "\n")

    print(f"backup={backup}")
    print(f"updated_rows={len(set(changed))}")
    print(f"unresolved_address_report={report}")
    print(f"unresolved_address_count={len(unresolved_address)}")


if __name__ == "__main__":
    main()
