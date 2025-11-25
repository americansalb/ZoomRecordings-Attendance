from typing import List, Dict, Tuple
from fuzzywuzzy import fuzz
from dataclasses import dataclass


@dataclass
class DuplicateMatch:
    """Represents a potential duplicate match between two profiles"""
    profile1_row: int
    profile2_row: int
    profile1_name: str
    profile2_name: str
    confidence: int  # 0-100
    reason: str


class DuplicateDetector:
    """Service for detecting potential duplicate profiles"""

    # Thresholds for matching
    NAME_SIMILARITY_THRESHOLD = 80  # Fuzzy match threshold
    EMAIL_MATCH_THRESHOLD = 90

    def find_duplicates(self, profiles: List[Dict]) -> List[DuplicateMatch]:
        """
        Find potential duplicate profiles

        Args:
            profiles: List of profile dicts with first_name, last_name, email, row_number

        Returns:
            List of DuplicateMatch objects sorted by confidence (highest first)
        """
        duplicates = []

        for i, profile1 in enumerate(profiles):
            for profile2 in profiles[i + 1:]:
                match = self._check_match(profile1, profile2)
                if match:
                    duplicates.append(match)

        # Sort by confidence (highest first)
        duplicates.sort(key=lambda x: x.confidence, reverse=True)
        return duplicates

    def _check_match(self, p1: Dict, p2: Dict) -> DuplicateMatch | None:
        """Check if two profiles might be duplicates"""
        name1 = f"{p1['first_name']} {p1['last_name']}".strip().lower()
        name2 = f"{p2['first_name']} {p2['last_name']}".strip().lower()

        # Check email match
        if p1.get('email') and p2.get('email'):
            email_similarity = fuzz.ratio(
                p1['email'].lower(),
                p2['email'].lower()
            )
            if email_similarity >= self.EMAIL_MATCH_THRESHOLD:
                return DuplicateMatch(
                    profile1_row=p1['row_number'],
                    profile2_row=p2['row_number'],
                    profile1_name=name1,
                    profile2_name=name2,
                    confidence=email_similarity,
                    reason="Similar email addresses"
                )

        # Check full name similarity
        full_name_similarity = fuzz.ratio(name1, name2)
        if full_name_similarity >= self.NAME_SIMILARITY_THRESHOLD:
            return DuplicateMatch(
                profile1_row=p1['row_number'],
                profile2_row=p2['row_number'],
                profile1_name=name1,
                profile2_name=name2,
                confidence=full_name_similarity,
                reason="Similar full names"
            )

        # Check for name variations (first/last swapped, nicknames, etc.)
        # First name matches last name
        if p1['first_name'].lower() == p2['last_name'].lower() and \
           p1['last_name'].lower() == p2['first_name'].lower():
            return DuplicateMatch(
                profile1_row=p1['row_number'],
                profile2_row=p2['row_number'],
                profile1_name=name1,
                profile2_name=name2,
                confidence=85,
                reason="First/last name swapped"
            )

        # Check partial matches (one name matches)
        first_match = fuzz.ratio(p1['first_name'].lower(), p2['first_name'].lower())
        last_match = fuzz.ratio(p1['last_name'].lower(), p2['last_name'].lower())

        if first_match >= 90 and last_match >= 70:
            return DuplicateMatch(
                profile1_row=p1['row_number'],
                profile2_row=p2['row_number'],
                profile1_name=name1,
                profile2_name=name2,
                confidence=int((first_match + last_match) / 2),
                reason="Similar first name, close last name"
            )

        if last_match >= 90 and first_match >= 70:
            return DuplicateMatch(
                profile1_row=p1['row_number'],
                profile2_row=p2['row_number'],
                profile1_name=name1,
                profile2_name=name2,
                confidence=int((first_match + last_match) / 2),
                reason="Similar last name, close first name"
            )

        # Check for common nickname patterns
        nickname_match = self._check_nicknames(p1, p2)
        if nickname_match:
            return nickname_match

        return None

    def _check_nicknames(self, p1: Dict, p2: Dict) -> DuplicateMatch | None:
        """Check for common nickname variations"""
        # Common nickname mappings
        nicknames = {
            'william': ['will', 'bill', 'billy', 'willy'],
            'robert': ['rob', 'bob', 'bobby', 'robbie'],
            'richard': ['rick', 'dick', 'rich', 'ricky'],
            'michael': ['mike', 'mikey', 'mick'],
            'james': ['jim', 'jimmy', 'jamie'],
            'joseph': ['joe', 'joey'],
            'thomas': ['tom', 'tommy'],
            'christopher': ['chris', 'topher'],
            'matthew': ['matt', 'matty'],
            'daniel': ['dan', 'danny'],
            'david': ['dave', 'davey'],
            'anthony': ['tony', 'ant'],
            'elizabeth': ['liz', 'lizzy', 'beth', 'betty', 'eliza'],
            'jennifer': ['jen', 'jenny', 'jenn'],
            'margaret': ['maggie', 'meg', 'peggy', 'marge'],
            'katherine': ['kate', 'katie', 'kathy', 'kat'],
            'patricia': ['pat', 'patty', 'trish'],
            'jessica': ['jess', 'jessie'],
            'stephanie': ['steph', 'stephie'],
            'alexandra': ['alex', 'lexi', 'sandra'],
            'alexander': ['alex', 'xander', 'zander'],
            'benjamin': ['ben', 'benji', 'benny'],
            'nicholas': ['nick', 'nicky'],
            'jonathan': ['jon', 'john', 'johnny'],
            'samuel': ['sam', 'sammy'],
            'timothy': ['tim', 'timmy'],
            'andrew': ['andy', 'drew'],
            'joshua': ['josh'],
            'zachary': ['zach', 'zack'],
            'nathaniel': ['nate', 'nathan', 'nat'],
        }

        first1 = p1['first_name'].lower()
        first2 = p2['first_name'].lower()

        # Check if one is a nickname of the other
        for full_name, nicks in nicknames.items():
            all_variants = [full_name] + nicks
            if first1 in all_variants and first2 in all_variants and first1 != first2:
                # Check if last names match
                if fuzz.ratio(p1['last_name'].lower(), p2['last_name'].lower()) >= 85:
                    name1 = f"{p1['first_name']} {p1['last_name']}"
                    name2 = f"{p2['first_name']} {p2['last_name']}"
                    return DuplicateMatch(
                        profile1_row=p1['row_number'],
                        profile2_row=p2['row_number'],
                        profile1_name=name1,
                        profile2_name=name2,
                        confidence=82,
                        reason=f"Possible nickname variation ({first1}/{first2})"
                    )

        return None


# Singleton instance
duplicate_detector = DuplicateDetector()
