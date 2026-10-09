"""Set B: generated emergency-screen cases.  Written from situations (who texts this number and why), not from the regex.
label: E = should trigger the emergency path; N = should not."""

CASES = [
 # --- wrong number, right now
 ("E","cardiac","my chest hurts real bad and my left arm is tingly is this the nurse"),
 ("E","cardiac","having a lot of pressure in my chest since lunch, sweating. what do i do"),
 ("E","cardiac","jaw and back are killing me and i feel sick to my stomach, im 68"),
 ("E","breathing","cant get a full breath. been like this an hour"),
 ("E","breathing","im wheezing bad and my inhaler isnt helping"),
 ("E","stroke","my mouth feels funny on one side and i cant say words right"),
 ("E","stroke","hi this is his daughter, dad is confused and one side of his face looks wrong"),
 ("E","stroke","suddenly cant see out of my right eye and dizzy"),
 ("E","fall","mom fell in the bathroom and i cant get her up shes not really answering me"),
 ("E","fall","im on the kitchen floor cant get up hit my head"),
 ("E","hypoglycemia","fasted like u said now im shaky n sweaty n cant think strait"),
 ("E","hypoglycemia","sugar reading says 38 i feel weird"),
 ("E","overdose","i took the whole bottle of my pills about an hour ago"),
 ("E","overdose","think i took my blood pressure meds twice by mistake and now im dizzy and my heart is racing"),
 ("E","suicidal","honestly i dont see the point of the test or anything else anymore"),
 ("E","suicidal","dont bother scheduling. i wont be around"),
 ("E","suicidal","i have a plan to end it tonight"),
 ("E","bleeding","cut my hand pretty deep its not stopping with the towel"),
 ("E","bleeding","im pregnant and bleeding a lot with cramps"),
 ("E","other-person","my husband just collapsed and hes breathing weird what do i do"),
 ("E","other-person","my neighbor is slumped in her car not moving"),
 ("E","allergic","lips are swelling up after i ate shrimp and my throat feels tight"),
 ("E","post-draw","just had the blood draw at brunswick and i blacked out in the parking lot, woke up on the ground"),
 ("E","post-draw","did the draw and now the site is bleeding thru 2 bandages and wont quit"),
 ("E","in-passing","thursday works if my arm stops going numb lol its been like that since this morning"),
 ("E","in-passing","can do friday. also been having crushing chest pain on and off today should i still come"),
 ("E","seizure","my son is shaking all over and wont respond"),
 ("E","confusion","i dont know where i am. this phone had your number"),
 ("E","cardiac","heart is pounding out of my chest and i feel like im going to pass out"),
 ("E","breathing","grandma is turning blue around the lips"),
 # --- alarming words, not an emergency
 ("N","past-event","i had a stroke in 2019 so i need the entrance without stairs"),
 ("N","past-event","was in the ER last month for chest pain, all clear now, can i still do the fasting one"),
 ("N","past-event","i have a heart condition so i cant fast too long, is 10am ok"),
 ("N","question","what happens if i faint during the draw, i always get lightheaded"),
 ("N","question","i have a cold should i still come or reschedule"),
 ("N","question","if i feel dizzy after can i sit for a bit before driving"),
 ("N","idiom","these reminders are giving me a heart attack, stop"),
 ("N","idiom","the parking at that place is a nightmare"),
 ("N","idiom","this bill is going to kill me"),
 ("N","idiom","im dying to get this over with, thursday"),
 ("N","admin","not an emergency but can someone call me about the cost"),
 ("N","admin","my emergency contact changed, its my daughter now 207 555 0142"),
 ("N","admin","in case of emergency who do i call at the lab"),
 ("N","angry","this is the 4th text. i said stop. im not coming"),
 ("N","angry","you people are a pain in my ass"),
 ("N","occupation","i work night shift in the ER at midcoast so mornings are bad"),
 ("N","occupation","i drive an ambulance, my schedule is all over the place"),
 ("N","deceased","this is her son. my mother passed away in july. please stop texting this number"),
 ("N","mild-symptom","i have a headache today, is it ok to take tylenol before the blood test"),
 ("N","mild-symptom","my arm was sore for a day after the last draw, is that normal"),
 ("N","scheduling","thursday after 5 is the only day that works for me"),
 ("N","scheduling","whats the address of the bath one"),
 ("N","other","my dog had a seizure last night so i cant come tomorrow, can we do next week"),
 ("N","other","i watched a show last night about a heart attack and now im paranoid. anyway, friday?"),
 ("N","other","the 911 operator training class i teach runs till 4 so after that"),
]

def main():
    from ocp.rules import emergency_signal
    tp=fp=fn=tn=0; misses=[]; false_alarms=[]
    for label,kind,text in CASES:
        fired = emergency_signal(text)
        if label=="E" and fired: tp+=1
        elif label=="E": fn+=1; misses.append((kind,text))
        elif fired: fp+=1; false_alarms.append((kind,text))
        else: tn+=1
    print(f"cases={len(CASES)}  emergencies={tp+fn}  non={fp+tn}")
    print(f"sensitivity (recall)  = {tp}/{tp+fn} = {tp/(tp+fn):.0%}")
    print(f"specificity           = {tn}/{tn+fp} = {tn/(tn+fp):.0%}")
    print(f"PPV (precision)       = {tp}/{tp+fp} = {tp/(tp+fp):.0%}" if tp+fp else "")
    print("\nMISSED (should fire, didn't):")
    for k,t in misses: print(f"  [{k}] {t}")
    print("\nFALSE ALARMS (fired, shouldn't):")
    for k,t in false_alarms: print(f"  [{k}] {t}")

if __name__=="__main__": main()
