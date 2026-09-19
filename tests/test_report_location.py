"""
The report header's 'City, State' label. GST-certificate and Udyam
addresses arrive as labelled fields; the label fragment 'Town' (from
'City/Town/Village') was being picked as the city, so every report read
"Town, <State>" (18-Sep, Sri Laxmi Steel and B R Trading Co).
"""
from vdd.report.render import location

GST_LAXMI = ("Floor No.: survey no 305 306 308 Building No./Flat No.: plot no 384 385 Name Of Premises/Building: "
             "s v co op industrial estate Road/Street: Road Number 2 Locality/Sub Locality: ida jeedimetla "
             "City/Town/Village: Hyderabad District: Medchal Malkajgiri State: Telangana PIN Code: 500055")
GST_BRT = ("Building No./Flat No.: 135/11/A/2 Road/Street: GIRISH GHOSH ROAD City/Town/Village: BELURMATH "
           "District: Howrah State: West Bengal PIN Code: 711202")
UDYAM_LAXMI = ("Flat/Door/Block No.: sy no 301 Name of Premises/ Building: plot 558 562 563 Village/Town: ram reddy nagar "
               "Road/Street/Lane: ida jeedimetla City: HYDERABAD State: TELANGANA District: HYDERABAD , Pin 500055")


def test_gst_certificate_address_gives_city_and_state():
    assert location(GST_LAXMI) == "Hyderabad, Telangana"
    assert location(GST_BRT) == "Belurmath, West Bengal"


def test_udyam_address_prefers_city_over_village_and_normalises_case():
    assert location(UDYAM_LAXMI) == "Hyderabad, Telangana"


def test_district_stands_in_when_no_city_label():
    assert location("District: Howrah State: West Bengal PIN Code: 711202") == "Howrah, West Bengal"


def test_unlabelled_address_still_uses_the_comma_heuristic():
    assert location("Plot 5, MIDC Bhosari, Pune - 411026, Maharashtra") == "Pune, Maharashtra"


def test_the_label_word_is_never_the_city():
    for addr in (GST_LAXMI, GST_BRT, UDYAM_LAXMI):
        assert not location(addr).startswith(("Town", "Village", "City"))
